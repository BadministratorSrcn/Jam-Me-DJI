package com.tactical.pilot

import android.app.Application
import dji.v5.common.error.IDJIError
import dji.v5.common.register.DJISDKInitEvent
import dji.v5.manager.SDKManager
import dji.v5.manager.interfaces.SDKManagerCallback

/**
 * Entry point — initializes DJI MSDK V5 before anything touches the aircraft.
 *
 * The App Key is read by the SDK from the manifest meta-data
 * (com.dji.sdk.API_KEY). Registration needs internet on the first run.
 */
class App : Application() {

    /**
     * DJI MSDK 5.18 ships its implementation as an encrypted in-memory dex.
     * Helper.install() loads libSdkyclx_clx.so and installs the real classes
     * before the rest of the app touches SDKManager. This must happen in
     * attachBaseContext(), not onCreate().
     */
    override fun attachBaseContext(base: android.content.Context?) {
        super.attachBaseContext(base)
        com.cySdkyc.clx.Helper.install(this)
    }

    override fun onCreate() {
        super.onCreate()
        instance = this

        // The packed SDK is arm64/armeabi only. If Helper.install() could not
        // load its native payload, fail visibly instead of crashing the UI.
        if (!sdkClassesAvailable()) {
            status.logLine("MSDK classes NOT loaded (packed SDK/ABI problem) — check logcat")
            return
        }

        val sdk = SDKManager.getInstance()
        status.logLine("MSDK init requested (${sdk.getSDKVersion()})")

        sdk.init(this, object : SDKManagerCallback {
            override fun onRegisterSuccess() {
                registered = true
                status.logLine("MSDK registered OK")
            }

            override fun onRegisterFailure(error: IDJIError) {
                registered = false
                status.logLine("MSDK register FAILED: ${error.errorCode()} ${error.description()}")
            }

            override fun onProductDisconnect(productId: Int) {
                productConnected = false
                status.logLine("product disconnected (id=$productId)")
            }

            override fun onProductConnect(productId: Int) {
                productConnected = true
                status.logLine("product connected (id=$productId)")
            }

            override fun onProductChanged(productId: Int) {
                status.logLine("product changed (id=$productId)")
            }

            override fun onInitProcess(event: DJISDKInitEvent, progress: Int) {
                status.logLine("MSDK init $event ($progress%)")
                if (event == DJISDKInitEvent.INITIALIZE_COMPLETE) {
                    // The sample calls registerApp() only after initialization
                    // completes; init() alone does not register the app.
                    status.logLine("MSDK initialize complete — registerApp()")
                    sdk.registerApp()
                }
            }

            override fun onDatabaseDownloadProgress(oldProgress: Long, newProgress: Long) {
                // The SDK downloads a flight-safety database in the background.
                if (newProgress == 100L) status.logLine("MSDK database ready")
            }
        })
    }

    companion object {
        var instance: App? = null

        @Volatile var registered = false
        @Volatile var productConnected = false
    }

    private fun sdkClassesAvailable(): Boolean = try {
        Class.forName("dji.v5.manager.SDKManager", false, classLoader)
        true
    } catch (_: Throwable) {
        false
    }
}

/** Tiny UI logger shared by components. */
object status {
    private val buf = mutableListOf<String>()

    @Synchronized
    fun lines(): List<String> = buf.toList()

    @Synchronized
    fun logLine(s: String) {
        buf.add(0, "${java.text.SimpleDateFormat("HH:mm:ss").format(java.util.Date())}  $s")
        while (buf.size > 60) buf.removeAt(buf.size - 1)
    }

    @Synchronized
    fun clear() {
        buf.clear()
    }
}
