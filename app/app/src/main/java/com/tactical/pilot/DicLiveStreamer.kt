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
import dji.v5.manager.datacenter.livestream.settings.RtspSettings
import java.net.URI

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
            status.logLine("STREAM error: ${error.description()}")
        }
    }

    fun isRunning(): Boolean = running

    fun statusText(): String = lastStatus

    fun toggle(url: String, onState: (Boolean) -> Unit) {
        if (running) stop(onState) else start(url, onState)
    }

    fun start(url: String, onState: (Boolean) -> Unit) {
        if (running) {
            onState(true)
            return
        }
        val isRtsp = url.startsWith("rtsp://", ignoreCase = true)
        try {
            val builder = LiveStreamSettings.Builder()
            if (isRtsp) {
                val uri = try { URI(url) } catch (_: Exception) { null }
                val userInfo = uri?.userInfo?.split(":") ?: emptyList()
                val port = uri?.port?.takeIf { it > 0 } ?: 8554
                builder.setLiveStreamType(LiveStreamType.RTSP)
                    .setRtspSettings(
                        RtspSettings.Builder()
                            .setUserName(userInfo.getOrNull(0) ?: "jamme")
                            .setPassWord(userInfo.getOrNull(1) ?: "jamme")
                            .setPort(port)
                            .build()
                    )
            } else {
                builder.setLiveStreamType(LiveStreamType.RTMP)
                    .setRtmpSettings(RtmpSettings.Builder().setUrl(url).build())
            }
            val settings = builder.build()

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
                    status.logLine("${if (isRtsp) "RTSP" else "RTMP"} start OK -> $url")
                    onState(true)
                }

                override fun onFailure(error: IDJIError) {
                    val already = error.errorCode()
                        ?.contains("ALREADY_STARTED", ignoreCase = true) == true
                    running = already
                    status.logLine("STREAM start FAILED: ${error.errorCode()} ${error.description()}")
                    onState(already)
                }
            })
        } catch (e: Exception) {
            running = false
            status.logLine("STREAM start exception: ${e.message?.take(80)}")
            onState(false)
        }
    }

    fun stop(onState: (Boolean) -> Unit) {
        try {
            mgr.stopStream(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    running = false
                    status.logLine("STREAM stopped")
                    onState(false)
                }

                override fun onFailure(error: IDJIError) {
                    status.logLine("STREAM stop FAILED: ${error.description()}")
                    onState(running)
                }
            })
        } catch (e: Exception) {
            running = false
            status.logLine("STREAM stop exception: ${e.message?.take(80)}")
            onState(false)
        }
    }
}
