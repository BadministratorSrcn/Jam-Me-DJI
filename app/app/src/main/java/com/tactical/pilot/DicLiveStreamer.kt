package com.tactical.pilot

import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.common.video.channel.VideoChannelType
import dji.v5.manager.datacenter.livestream.LiveStreamManager
import dji.v5.manager.datacenter.livestream.LiveStreamSettings
import dji.v5.manager.datacenter.livestream.LiveStreamStatus
import dji.v5.manager.datacenter.livestream.LiveStreamStatusListener
import dji.v5.manager.datacenter.livestream.LiveStreamType
import dji.v5.manager.datacenter.livestream.LiveVideoBitrateMode
import dji.v5.manager.datacenter.livestream.StreamQuality
import dji.v5.manager.datacenter.livestream.settings.RtmpSettings

/**
 * Streams the Mini 4 Pro camera to the ground PC through MSDK's
 * LiveStreamManager. The PC runs an RTMP server (mediamtx) and the bridge
 * pulls the stream from rtmp://127.0.0.1:1935/live.
 */
class DicLiveStreamer {

    private val mgr = LiveStreamManager.getInstance()

    @Volatile private var running = false
    @Volatile private var lastStatus = ""

    private val listener = object : LiveStreamStatusListener {
        override fun onLiveStreamStatusUpdate(st: LiveStreamStatus) {
            running = st.isStreaming
            val res = st.resolution
            lastStatus = "%dfps  %dkbps  %sx%s  loss=%d".format(
                st.fps, st.vbps / 1000, res?.width ?: 0, res?.height ?: 0, st.packetLoss
            )
        }

        override fun onError(error: IDJIError) {
            status.logLine("RTMP error: ${error.description()}")
        }
    }

    fun isRunning(): Boolean = running

    fun statusText(): String = lastStatus

    fun toggle(rtmpUrl: String, onState: (Boolean) -> Unit) {
        if (running) stop(onState) else start(rtmpUrl, onState)
    }

    fun start(url: String, onState: (Boolean) -> Unit) {
        if (running) {
            onState(true)
            return
        }
        try {
            val settings = LiveStreamSettings.Builder()
                .setLiveStreamType(LiveStreamType.RTMP)
                .setRtmpSettings(RtmpSettings.Builder().setUrl(url).build())
                .build()

            mgr.setLiveStreamSettings(settings)
            mgr.setVideoChannelType(VideoChannelType.PRIMARY_STREAM_CHANNEL)
            mgr.setCameraIndex(ComponentIndexType.LEFT_OR_MAIN)
            mgr.setLiveStreamQuality(StreamQuality.HD)
            mgr.setLiveVideoBitrateMode(LiveVideoBitrateMode.AUTO)
            mgr.removeLiveStreamStatusListener(listener)
            mgr.addLiveStreamStatusListener(listener)
            mgr.startStream(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    running = true
                    status.logLine("RTMP start OK -> $url")
                    onState(true)
                }

                override fun onFailure(error: IDJIError) {
                    running = false
                    status.logLine("RTMP start FAILED: ${error.errorCode()} ${error.description()}")
                    onState(false)
                }
            })
        } catch (e: Exception) {
            running = false
            status.logLine("RTMP start exception: ${e.message?.take(80)}")
            onState(false)
        }
    }

    fun stop(onState: (Boolean) -> Unit) {
        try {
            mgr.stopStream(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    running = false
                    status.logLine("RTMP stopped")
                    onState(false)
                }

                override fun onFailure(error: IDJIError) {
                    status.logLine("RTMP stop FAILED: ${error.description()}")
                    onState(running)
                }
            })
        } catch (e: Exception) {
            running = false
            status.logLine("RTMP stop exception: ${e.message?.take(80)}")
            onState(false)
        }
    }
}
