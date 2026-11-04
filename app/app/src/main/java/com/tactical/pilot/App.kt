package com.tactical.pilot

import android.app.Application

/**
 * Entry point — initializes DJI MSDK V5 before anything touches the aircraft.
 *
 * The App Key is read by the SDK from the manifest meta-data
 * (com.dji.sdk.API_KEY). Registration needs internet on the first run.
 */
class App : Application() {

    override fun onCreate() {
        super.onCreate()
        instance = this

        installPackedSdk()

        // Keep SDK references out of this class: if the packed SDK failed to
        // load, class verification of a callback would crash before we can
        // show the error. SdkBootstrap is only loaded when classes exist.
        if (!sdkClassesAvailable()) {
            status.logLine("MSDK classes NOT loaded (packed SDK/ABI problem) — check logcat")
            return
        }

        SdkBootstrap.init(this)
    }

    /**
     * MSDK 5.18 ships its implementation as an encrypted in-memory dex and
     * installs it from Helper.install(). Keep the official path; the
     * Android 16/HyperOS Parcel bug is handled by the targetSdk compat path
     * and the SdkBootstrap isolation.
     */
    private fun installPackedSdk() {
        try {
            com.cySdkyc.clx.Helper.install(this)
        } catch (t: Throwable) {
            status.logLine("DJI Helper.install failed: ${t.message?.take(90)}")
        }
    }

    companion object {
        var instance: App? = null

        @Volatile var registered = false
        @Volatile var productConnected = false
    }

    private fun sdkClassesAvailable(): Boolean = try {
        Class.forName("dji.v5.manager.SDKManager", false, classLoader)
        Class.forName("dji.v5.manager.interfaces.SDKManagerCallback", false, classLoader)
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
        android.util.Log.i("TacticalPilot", s)
        buf.add(0, "${java.text.SimpleDateFormat("HH:mm:ss").format(java.util.Date())}  $s")
        while (buf.size > 60) buf.removeAt(buf.size - 1)
    }

    @Synchronized
    fun clear() {
        buf.clear()
    }
}
