plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val build = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "app.outfit"
    compileSdk = 34
    defaultConfig {
        applicationId = "app.outfit"
        minSdk = 28 // ImageDecoder: applies the photo's EXIF rotation, so pose detection sees people upright
        targetSdk = 34
        versionCode = build
        versionName = "1.0.$build"
    }
    // Same signing setup as the Expense Sync app: the key comes from the build, never from the repository.
    signingConfigs {
        create("shared") {
            storeFile = file(System.getenv("SIGNING_STORE") ?: "release.keystore")
            storePassword = System.getenv("SIGNING_PASSWORD")
            keyAlias = System.getenv("SIGNING_ALIAS") ?: "expense-sync"
            keyPassword = System.getenv("SIGNING_PASSWORD")
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("shared")
        }
    }
    androidResources { noCompress += listOf("tflite", "task") }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    lint { checkReleaseBuilds = false }
}

dependencies {
    implementation("com.google.mediapipe:tasks-vision:0.10.14")
    testImplementation("junit:junit:4.13.2")
}
