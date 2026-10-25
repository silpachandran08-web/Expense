plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val build = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "app.expense.sync"
    compileSdk = 34
    defaultConfig {
        applicationId = "app.expense.sync"
        minSdk = 26
        targetSdk = 34
        versionCode = build
        versionName = "1.0.$build"
    }
    // The signing key never lives in the repository: the build passes it in (from a GitHub secret, or a
    // key made for that one build when no secret is set).
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
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    lint { checkReleaseBuilds = false }
}

dependencies {
    implementation("androidx.work:work-runtime-ktx:2.9.1")
}
