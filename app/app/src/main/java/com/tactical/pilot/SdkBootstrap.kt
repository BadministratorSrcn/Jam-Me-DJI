package com.tactical.pilot

import dji.v5.common.error.IDJIError
import dji.v5.common.register.DJISDKInitEvent
import dji.v5.manager.SDKManager
import dji.v5.manager.interfaces.SDKManagerCallback

/**
 * Isolated MSDK bootstrap. This class is only loaded when the packed SDK's
 * classes are actually available, so a failed Helper.install() cannot crash
 * the Application during class verification.
 */
object SdkBootstrap {

    fun init(app: App) {
        val sdk = SDKManager.getInstance()
        status.logLine("MSDK init requested (${sdk.getSDKVersion()})")

        sdk.init(app, object : SDKManagerCallback {
            override fun onRegisterSuccess() {
                App.registered = true
                status.logLine("MSDK registered OK")
            }

            override fun onRegisterFailure(error: IDJIError) {
                App.registered = false
                status.logLine("MSDK register FAILED: ${error.errorCode()} ${error.description()}")
            }

            override fun onProductDisconnect(productId: Int) {
                App.productConnected = false
                status.logLine("product disconnected (id=$productId)")
            }

            override fun onProductConnect(productId: Int) {
                App.productConnected = true
                status.logLine("product connected (id=$productId)")
            }

            override fun onProductChanged(productId: Int) {
                status.logLine("product changed (id=$productId)")
            }

            override fun onInitProcess(event: DJISDKInitEvent, progress: Int) {
                status.logLine("MSDK init $event ($progress%)")
                if (event == DJISDKInitEvent.INITIALIZE_COMPLETE) {
                    status.logLine("MSDK initialize complete — registerApp()")
                    sdk.registerApp()
                }
            }

            override fun onDatabaseDownloadProgress(oldProgress: Long, newProgress: Long) {
                if (newProgress == 100L) status.logLine("MSDK database ready")
            }
        })
    }

    fun retryRegister() {
        SDKManager.getInstance().registerApp()
    }
}
