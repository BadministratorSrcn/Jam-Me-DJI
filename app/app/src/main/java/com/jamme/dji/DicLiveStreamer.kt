package com.jamme.dji

/**
 * Starts/pauses the Mini 4 Pro camera live streaming toward the ground
 * PC through MSDK's LiveStreamManager.
 *
 * The exact API names follow MSDK V5 LiveStreamManager; use the official
 * "Live Stream Sample" (developer.dji.com/doc/mobile-sdk-tutorial/
 * en/tutorials/live-stream.html) as reference for the version you ship.
 *
 * Skeleton contract here (real code wired when AAR added):
 *   start(url) coroutine-ish blocking calls:
 *     liveStreamManager.setLiveStreamSettings(...)
 *     liveStreamManager.start()
 *   stop(): liveStreamManager.stop()
 */
class DicLiveStreamer(private val activity: Activity) {

    private var running = false

    fun toggle(rtmpUrl: String, onState: (Boolean) -> Unit) {
        // SKELETON — wire with real LiveStreamManager calls in app/libs phase
        status.logLine("RTMP ${if (running) "stop" else "start"}: $rtmpUrl (wire MSDK LiveStreamManager here)")
        running = !running
        onState(running)
    }

    fun isRunning(): Boolean = running
}

private typealias Activity = android.app.Activity
