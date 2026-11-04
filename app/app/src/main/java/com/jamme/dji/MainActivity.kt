package com.jamme.dji

import android.app.Activity
import android.os.Bundle
import android.widget.LinearLayout
import android.widget.TextView
import android.view.View
import android.widget.Button

/**
 * HUD — single activity showing, live:
 *   • MSDK registration/connection status
 *   • DJI-GPS vs visual-fix divergence (from the ground PC bridge)
 *   • RTMP streaming controls
 *   • Telemetry log
 *
 * This is a deliberately minimal skeleton: production UI belongs to
 * UXSDK or a proper layout; here we keep everything programmatic so the
 * repo compiles (after you drop the MSDK AARs into app/libs and paste
 * your App Key) without any designer assets.
 */
class MainActivity : Activity() {

    private lateinit var statusView: TextView
    private lateinit var posView: TextView
    private lateinit var streamBtn: Button
    private lateinit var demoBtn: Button
    private lateinit var client: VisualPosClient
    private var uiAlive = true

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        statusView = TextView(this).apply { text = status.lines.joinToString("\n") }
        posView = TextView(this).apply { textSize = 18f }
        streamBtn = Button(this).apply { text = "RTMP stream: START" }
        demoBtn = Button(this).apply { text = "Re-poll bridge now" }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(statusView)
            addView(posView)
            addView(streamBtn)
            addView(demoBtn)
        }
        setContentView(root)

        client = VisualPosClient(host = "192.168.32.123", port = 8080)  // ← your PC's hotspot IP
        streamBtn.setOnClickListener { DicLiveStreamer(this).toggle(
            rtmpUrl = "rtmp://192.168.32.123:1935/live",
            onState = { ok -> runOnUiThread { streamBtn.text = "RTMP stream: ${if (ok) "RUNNING" else "IDLE"}" } }) }
        demoBtn.setOnClickListener { client.refreshOnce() }

        // background UI refresh loop
        Thread {
            while (uiAlive) {
                client.poll()
                runOnUiThread {
                    statusView.text = status.lines.joinToString("\n")
                    posView.text = client.hudText()
                }
                Thread.sleep(300)
            }
        }.start()
        uiAlive = true
    }

    override fun onDestroy() {
        uiAlive = false
        super.onDestroy()
    }
}
