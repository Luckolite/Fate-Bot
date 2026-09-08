plugins {
    id("com.android.application")
}

android {
    namespace = "xyz.fatebot.status"
    compileSdk = 35

    defaultConfig {
        applicationId = "xyz.fatebot.status"
        minSdk = 26
        targetSdk = 35
        versionCode = 39
        versionName = "3.2.5"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
