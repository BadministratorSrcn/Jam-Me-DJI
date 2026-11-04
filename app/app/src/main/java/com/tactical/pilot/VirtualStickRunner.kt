package com.tactical.pilot

import android.os.SystemClock
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.flightcontroller.FlightCoordinateSystem
import dji.sdk.keyvalue.value.flightcontroller.RollPitchControlMode
import dji.sdk.keyvalue.value.flightcontroller.VerticalControlMode
import dji.sdk.keyvalue.value.flightcontroller.VirtualStickFlightControlParam
import dji.sdk.keyvalue.value.flightcontroller.YawControlMode
import dji.sdk.keyvalue.value.flightcontroller.FlightControlAuthorityChangeReason
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.aircraft.virtualstick.VirtualStickManager
import dji.v5.manager.aircraft.virtualstick.VirtualStickState
import dji.v5.manager.aircraft.virtualstick.VirtualStickStateListener

/**
 * Polls the ground bridge (GET /drive) and maps the bridge-computed body
 * velocities into DJI VirtualStick commands at 10 Hz.
 *
 * Fail-safe rules enforced locally, independent of the bridge:
 *   • no fresh /drive (age > 800 ms)  -> zero velocities
 *   • bridge says blind or non-go/hold -> zero velocities
 *   • virtual-stick authority lost    -> disarm the loop immediately
 *
 * The aircraft is never commanded from DJI GPS; /drive is computed from the
 * visual tile fix on the PC.
 */
class VirtualStickRunner(
    private val client: VisualPosClient,
    private val onState: (String) -> Unit
) {

    private val vsm = VirtualStickManager.getInstance()
    private var worker: Thread? = null

    @Volatile private var armed = false
    @Volatile private var hasAuthority = false
    private var vsInitialized = false
    @Volatile private var lastSentVf = 0.0
    @Volatile private var lastSentVr = 0.0

    // Sign constants — verified on the bench with propellers removed.
    // DJI BODY frame: +pitch = forward, +roll = right.
    var pitchSign = 1.0
    var rollSign = 1.0
    var vmax = 2.5

    private val stateListener = object : VirtualStickStateListener {
        override fun onVirtualStickStateUpdate(state: VirtualStickState) {
            if (hasAuthority && !state.isVirtualStickEnable) {
                status.logLine("VirtualStick authority lost — loop stopped")
                stopLoop("LOST")
            }
        }

        override fun onChangeReasonUpdate(reason: FlightControlAuthorityChangeReason) {
            status.logLine("VirtualStick authority reason: $reason")
        }
    }

    fun isArmed(): Boolean = armed

    fun speedText(): String = "vf=%+.2f vr=%+.2f".format(lastSentVf, lastSentVr)

    fun arm() {
        if (armed) return
        if (!App.productConnected) {
            onState("NO PRODUCT")
            status.logLine("ARM refused: no aircraft connected")
            return
        }
        try {
            if (!vsInitialized) {
                vsm.init()
                vsInitialized = true
            }
            vsm.setVirtualStickAdvancedModeEnabled(true)
            vsm.setVirtualStickStateListener(stateListener)
            vsm.enableVirtualStick(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    armed = true
                    hasAuthority = true
                    status.logLine("VirtualStick ENABLED — PC has control")
                    onState("ARMED")
                    startLoop()
                }

                override fun onFailure(error: IDJIError) {
                    armed = false
                    hasAuthority = false
                    status.logLine("VirtualStick enable FAILED: ${error.errorCode()} ${error.description()}")
                    onState("FAILED")
                }
            })
        } catch (e: Exception) {
            status.logLine("VirtualStick arm exception: ${e.message?.take(90)}")
            onState("ERROR")
        }
    }

    fun armOnDefaultCamera() {
        // Kept for API compatibility with earlier skeleton; arm() is explicit.
    }

    fun disarm() {
        if (!armed) return
        stopLoop("IDLE")
        try {
            vsm.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    status.logLine("VirtualStick disabled — RC has control")
                }

                override fun onFailure(error: IDJIError) {
                    status.logLine("VirtualStick disable FAILED: ${error.description()}")
                }
            })
            vsm.removeVirtualStickStateListener(stateListener)
        } catch (e: Exception) {
            status.logLine("VirtualStick disarm exception: ${e.message?.take(80)}")
        }
    }

    private fun stopLoop(state: String) {
        armed = false
        hasAuthority = false
        send(0.0, 0.0)
        onState(state)
    }

    private fun startLoop() {
        worker = Thread {
            while (armed) {
                try {
                    val now = SystemClock.elapsedRealtime()
                    val d = client.lastDrive
                    val age = if (client.lastDriveAt == 0L) Long.MAX_VALUE else now - client.lastDriveAt
                    val mode = d?.optString("mode") ?: "idle"
                    val blind = d?.optBoolean("blind", false) ?: true

                    var vf = 0.0
                    var vr = 0.0
                    if (age <= 800 && !blind && (mode == "go" || mode == "hold")) {
                        vf = d?.optDouble("v_forward", 0.0)?.coerceIn(-vmax, vmax) ?: 0.0
                        vr = d?.optDouble("v_right", 0.0)?.coerceIn(-vmax, vmax) ?: 0.0
                    }
                    send(vf, vr)
                } catch (e: Exception) {
                    status.logLine("stick loop error: ${e.message?.take(70)}")
                }
                Thread.sleep(100)
            }
        }.also { it.start() }
    }

    private fun send(vf: Double, vr: Double) {
        try {
            val p = VirtualStickFlightControlParam()
            p.pitch = vf * pitchSign
            p.roll = vr * rollSign
            p.yaw = 0.0
            p.verticalThrottle = 0.0
            p.rollPitchControlMode = RollPitchControlMode.VELOCITY
            p.yawControlMode = YawControlMode.ANGULAR_VELOCITY
            p.verticalControlMode = VerticalControlMode.VELOCITY
            p.rollPitchCoordinateSystem = FlightCoordinateSystem.BODY
            vsm.sendVirtualStickAdvancedParam(p)
            lastSentVf = vf
            lastSentVr = vr
        } catch (e: Exception) {
            status.logLine("stick send error: ${e.message?.take(70)}")
        }
    }
}
