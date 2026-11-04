package com.tactical.pilot

/**
 * VirtualStick runner — the flight-control endpoint of the GPS-free loop.
 *
 * Polls the ground bridge (GET /drive every ~150 ms) and maps the
 * bridge-computed body velocities into DJI VirtualStick inputs at 10 Hz.
 * The bridge NEVER commands velocity without a fresh visual fix: when the
 * fix is stale it sends zeros (safe hold), so this class can simply obey
 * whatever the bridge publishes.
 *
 * == MSDK wiring (TODO when AARs are in app/libs) ==
 *   1. On first arm: FlightController virtual stick enable
 *        KeyManager.getInstance().setValue(
 *            FlightControllerKey.create(KEY_IS_VIRTUAL_STICK_ENABLED), true, ...)
 *   2. 10 Hz send loop:
 *        FlightControlData(pitch, roll, yaw, throttle)
 *        flightController.sendVirtualStickAdvancedParam(...) or
 *        KeyManager KEY_VIRTUAL_STICK_ADVANCED — use the official
 *        "Virtual Stick Sample" as reference for version-specific calls.
 *   3. On disarm/lost telemetry: send zeros, then disable virtual stick.
 *
 * == Sign conventions (VERIFY ON HARDWARE before first real flight) ==
 *   v_forward > 0  =>  fly along camera heading (north when heading=0)
 *   v_right   > 0  =>  fly to the right of heading
 *   DJI pitch/roll signs differ from the intuitive mapping on some
 *   firmware/product combos — run the on-bench probe and set the
 *   constants below accordingly.
 *
 * == Position-hold semantics ==
 *   /drive mode="hold" with (dist→0) velocities means the bridge keeps
 *   the aircraft on the captured visual anchor — the drone holds only as
 *   long as the tile fix lives; blind fix => zeros => DJI's own
 *   stabilization takes over (never blind commands).
 */
class VirtualStickRunner(
    private val client: VisualPosClient,
    private val onState: (String) -> Unit
) {

    @Volatile private var armed = false
    private var worker: Thread? = null

    // sign constants — verify with the on-bench probe first
    var pitchSign = -1f
    var rollSign = +1f
    var vmax = 2.5f          // must match bridge --vmax

    fun arm() {
        if (armed) return
        armed = true
        status.logLine("VirtualStick ARMED (PC control)")
        onState("ARMED")
        worker = Thread {
            while (armed) {
                try {
                    val d = client.lastDrive
                    if (d != null && (d["mode"] == "go" || d["mode"] == "hold")) {
                        val vf = (d["v_forward"] as? Double)?.toFloat() ?: 0f
                        val vr = (d["v_right"] as? Double)?.toFloat() ?: 0f
                        val pitch = clamp(vf / vmax, 1f) * pitchSign
                        val roll = clamp(vr / vmax, 1f) * rollSign
                        // TODO(MSDK): flightController.sendVirtualStick(pitch, roll, 0f, 0f)
                        if (((System.nanoTime() / 1e8).toInt() % 10) == 0) {
                            status.logLine("stick: pitch %+0.2f roll %+0.2f (mode ${d["mode"]})".format(pitch, roll))
                        }
                    } else {
                        // TODO(MSDK): sendVirtualStick(0f, 0f, 0f, 0f) keep-alive
                    }
                } catch (e: Exception) {
                    status.logLine("stick loop error: ${e.message?.take(60)}")
                }
                Thread.sleep(100)
            }
        }.also { it.start() }
    }

    fun disarm() {
        if (!armed) return
        armed = false
        // TODO(MSDK): flush zeros then disable virtual stick
        status.logLine("VirtualStick DISARMED")
        onState("IDLE")
    }

    fun isArmed() = armed
}

private fun clamp(v: Float, m: Float): Float = v.coerceIn(-m, m)
