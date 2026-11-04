package com.tactical.pilot

import android.app.Application
import android.content.Context
import dji.v5.manager.SDKManager
import dji.v5.manager.interfaces.SDKManagerCallback

/**
 * Entry point — initializes DJI MSDK V5 before anything touches the aircraft.
 *
 * Build note: use the official MSDK V5 sample (`dev-sdk-main`) as reference
 * for initialization details — it must be registered on first internet
 * connection, handle auth flows and RC model checks.
 */
class App : Application() {

    companion object {
        @JvmStatic var instance: App? = null
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        val apiKey = (applicationContext as? Context)?.takeIf { true }?.let {
            // api key lives in the manifest meta-data
            val ai = it.packageManager.getApplicationInfo(it.packageName,
                android.content.pm.PackageManager.GET_META_DATA)
            ai.metaData?.getString("com.dji.sdk.API_KEY") ?: ""
        } ?: ""

        SDKManager.getInstance().init(apiKey, object : SDKManagerCallback {
            override fun onRegisterSuccess() {
                status.logLine("MSDK registered OK")
            }
            override fun onRegisterFailure(error: String?) {
                status.logLine("MSDK registration FAILED: $error")
            }
            override fun onProductDisconnect(product: AnyObject?) { status.logLine("product disconnected") }
            override fun onProductConnect(product: dji.v5.common.ProductInfo?) {
                status.logLine("product connected: ${product?.productType}")
            }
            override fun onProductChanged(product: dji.v5.common.ProductType?) {}
            override fun onComponentChange(componentType: dji.v5.common.ComponentType?,
                                           old: dji.v5.common.DJIRelationComponent?,
                                           newValue: dji.v5.common.DJIRelationComponent?) {}
            override fun onInitProcess(event: dji.v5.manager.frame.InitProcessType?) {}
            override fun onDatabaseDownloadProgress(oldProgress: Int, newProgress: Int) {}
        })
    }
}

/** Tiny UI logger shared by components. */
object status {
    private val buf = mutableListOf<String>()
    val lines: List<String> get() = buf.toList()
    fun logLine(s: String) {
        buf.add(0, "${java.text.SimpleDateFormat("HH:mm:ss").format(java.util.Date())}  $s")
        while (buf.size > 40) buf.removeAt(buf.size - 1)
    }
}

private typealias AnyObject = Any
