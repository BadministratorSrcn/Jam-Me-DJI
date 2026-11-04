plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jamme.dji"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jamme.dji"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    packaging {
        jniLibs.pickFirsts.add("**/libmsdklg_android.so")
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
    // DJI MSDK V5 — drop the official sample's AARs into app/libs, e.g.:
    //   api-5.17.0.aar  (or newer — Mini 4 Pro RC-N3 needs >= 5.17)
    //   components-5.17.0.aar
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.aar"))))

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
