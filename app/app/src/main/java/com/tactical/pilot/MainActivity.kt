package com.tactical.pilot

import android.app.Activity
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import dji.sdk.keyvalue.key.GimbalKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotation
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotationMode
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.KeyManager
import dji.v5.manager.SDKManager

/**
 * Tactical Pilot HUD.
 *
 * The PC bridge owns the mission and the GPS-free control loop; this app is
 * the thin DJI endpoint: it streams camera video, presents telemetry, and
 * forwards /drive velocities to VirtualStick only while explicitly ARMED.
 */
class MainActivity : Activity() {

    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var rtmpEdit: EditText
    private lateinit var linkView: TextView
    private lateinit var telemetryView: TextView
    private lateinit var posView: TextView
    private lateinit var logView: TextView
    private lateinit var streamBtn: Button
    private lateinit var armBtn: Button

    private lateinit var client: VisualPosClient
    private lateinit var telemetry: TelemetryListener
    private var streamer: DicLiveStreamer? = null
    private var stick: VirtualStickRunner? = null
    private var uiAlive = true

    private val prefs by lazy { getSharedPreferences("tactical", MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Keep the screen (and the 10 Hz control loop) alive during flight.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val host = prefs.getString("host", "192.168.1.107") ?: "192.168.1.107"
        val port = prefs.getInt("port", 8080)
        val rtmp = prefs.getString("rtmp", "rtmp://$host:1935/live")
            ?: "rtmp://$host:1935/live"

        client = VisualPosClient(host, port)
        telemetry = TelemetryListener(client)

        hostEdit = EditText(this).apply {
            setText(host)
            hint = "PC IP"
            inputType = InputType.TYPE_CLASS_TEXT
        }
        portEdit = EditText(this).apply {
            setText(port.toString())
            hint = "bridge port"
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        rtmpEdit = EditText(this).apply {
            setText(rtmp)
            hint = "rtmp://PC-IP:1935/live"
            inputType = InputType.TYPE_CLASS_TEXT
        }
        linkView = TextView(this).apply { textSize = 13f }
        telemetryView = TextView(this).apply { textSize = 13f }
        posView = TextView(this).apply { textSize = 13f }
        logView = TextView(this).apply { textSize = 11f }

        val connectBtn = Button(this).apply { text = "CONNECT TO PC" }
        streamBtn = Button(this).apply { text = "RTMP STREAM: START" }
        val gimbalBtn = Button(this).apply { text = "CAMERA: POINT DOWN (-90°)" }
        armBtn = Button(this).apply { text = "PC CONTROL: ARM" }
        val retryBtn = Button(this).apply { text = "RETRY MSDK REGISTER" }
        val clearBtn = Button(this).apply { text = "CLEAR LOG" }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(24, 24, 24, 24)
            addView(TextView(this@MainActivity).apply {
                text = "TACTICAL PILOT — Mini 4 Pro endpoint"
                textSize = 20f
            })
            addView(hostEdit)
            addView(portEdit)
            addView(rtmpEdit)
            addView(connectBtn)
            addView(linkView)
            addView(telemetryView)
            addView(posView)
            addView(streamBtn)
            addView(gimbalBtn)
            addView(armBtn)
            addView(retryBtn)
            addView(clearBtn)
            addView(TextView(this@MainActivity).apply {
                text = "EVENT LOG"
                textSize = 14f
                setPadding(0, 18, 0, 6)
            })
            addView(logView)
        }
        setContentView(ScrollView(this).apply { addView(root) })

        connectBtn.setOnClickListener { applyConnection() }
        streamBtn.setOnClickListener {
            val s = streamer ?: DicLiveStreamer().also { streamer = it }
            s.toggle(rtmpEdit.text.toString().trim()) { ok ->
                runOnUiThread { streamBtn.text = "RTMP STREAM: ${if (ok) "RUNNING" else "START"}" }
            }
        }
        gimbalBtn.setOnClickListener { pointCameraDown() }
        retryBtn.setOnClickListener {
            try {
                SDKManager.getInstance().registerApp()
                status.logLine("MSDK registerApp() retry requested")
            } catch (e: Throwable) {
                status.logLine("registerApp retry failed: ${e.message?.take(70)}")
            }
            refreshUi()
        }
        armBtn.setOnClickListener {
            val s = stick
            if (s?.isArmed() == true) {
                s.disarm()
                armBtn.text = "PC CONTROL: ARM"
            } else {
                if (stick == null) {
                    stick = VirtualStickRunner(client) { state ->
                        runOnUiThread {
                            armBtn.text = "PC CONTROL: $state"
                            if (state != "ARMED") armBtn.text = "PC CONTROL: ARM"
                        }
                    }
                }
                stick?.arm()
            }
        }
        clearBtn.setOnClickListener {
            status.clear()
            refreshUi()
        }

        Thread {
            while (uiAlive) {
                client.poll()
                if (App.productConnected) {
                    // Attach MSDK key listeners only after the aircraft is
                    // actually connected; early listens can be dropped.
                    telemetry.start()
                }
                runOnUiThread { refreshUi() }
                Thread.sleep(250)
            }
        }.start()
    }

    private fun applyConnection() {
        val host = hostEdit.text.toString().trim()
        val port = portEdit.text.toString().trim().toIntOrNull() ?: 8080
        if (host.isEmpty()) return
        val oldHost = client.host
        // Keep the RTMP URL in sync when the operator only changes the PC IP.
        val currentRtmp = rtmpEdit.text.toString().trim()
        if (currentRtmp.isBlank() || currentRtmp.contains(oldHost)) {
            rtmpEdit.setText("rtmp://$host:1935/live")
        }
        client.host = host
        client.port = port
        prefs.edit()
            .putString("host", host)
            .putInt("port", port)
            .putString("rtmp", rtmpEdit.text.toString().trim())
            .apply()
        status.logLine("bridge target set to $host:$port")
        client.refreshOnce()
        refreshUi()
    }

    private fun pointCameraDown() {
        try {
            val rotation = GimbalAngleRotation().apply {
                mode = GimbalAngleRotationMode.ABSOLUTE_ANGLE
                pitch = -90.0
                roll = 0.0
                yaw = 0.0
                pitchIgnored = false
                rollIgnored = true
                yawIgnored = false
                duration = 1.5
                jointReferenceUsed = false
                timeout = 10
            }
            KeyManager.getInstance().performAction(
                KeyTools.createKey(GimbalKey.KeyRotateByAngle, ComponentIndexType.LEFT_OR_MAIN),
                rotation,
                object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                    override fun onSuccess(result: EmptyMsg) {
                        status.logLine("gimbal command sent: pitch -90°")
                    }

                    override fun onFailure(error: IDJIError) {
                        status.logLine("gimbal command FAILED: ${error.description()}")
                    }
                }
            )
        } catch (e: Throwable) {
            status.logLine("gimbal exception: ${e.message?.take(80)}")
        }
    }

    private fun refreshUi() {
        linkView.text = buildString {
            appendLine("PC LINK  ${if (client.lastPollOk) "OK" else "DOWN"}  " +
                    "status age ${ageText(client.statusAgeMs())}  drive age ${ageText(client.driveAgeMs())}")
            if (!client.lastPollOk) append("error: ${client.lastError ?: "—"}")
        }
        telemetryView.text = telemetry.hudLines()
        posView.text = client.hudText()
        logView.text = status.lines().joinToString("\n")
        if (streamer?.isRunning() == true) {
            streamBtn.text = "RTMP STREAM: RUNNING"
        }
        val s = stick
        if (s != null && s.isArmed()) {
            armBtn.text = "PC CONTROL: ARMED  ${s.speedText()}"
        }
    }

    private fun ageText(ms: Long): String = if (ms == Long.MAX_VALUE) "—" else "$ms ms"

    override fun onDestroy() {
        uiAlive = false
        try {
            stick?.disarm()
            streamer?.stop { }
        } catch (_: Exception) {
        }
        super.onDestroy()
    }
}
