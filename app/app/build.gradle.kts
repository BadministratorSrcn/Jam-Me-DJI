plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.tactical.pilot"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.tactical.pilot"   // must match the MSDK App Key registration
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    packaging {
        jniLibs.pickFirsts.add("**/libmsdklg_android.so")
        // DJI ships extractNativeLibs=true; keep the same packaging mode.
        jniLibs.useLegacyPackaging = true
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    // DJI MSDK V5.18.0. The aircraft AAR carries the native/in-memory SDK
    // payload; the "provided" jar is compile-only API stubs and must NOT be
    // packaged (Helper.install() loads the real classes at runtime).
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.aar"))))
    implementation("com.dji:dji-sdk-v5-aircraft:5.18.0")
    compileOnly("com.dji:dji-sdk-v5-aircraft-provided:5.18.0")
    runtimeOnly("com.dji:dji-sdk-v5-networkImp:5.18.0")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
