import java.util.Properties

plugins { id("com.android.application") }

val xheyFile = rootProject.file("xhey.properties")
val xhey = Properties().apply { if (xheyFile.isFile) xheyFile.reader(Charsets.UTF_8).use { load(it) } }
val vendorRoot = "xhey-sdk/1.3.70/XheyCameraSDK-v1.3.70.9b61b4a6.Android"
val xheyDir = rootProject.file(xhey.getProperty("sdkDir", vendorRoot))
val xheyAars = fileTree(xheyDir) { include("*.aar") }
val xheyAssets = rootProject.file(xhey.getProperty("assetsParent", vendorRoot))
val xheyPresent = !xheyAars.isEmpty && xheyAssets.resolve("XheyCameraSDKAssets").isDirectory
val cameraXVersion = xhey.getProperty("cameraXVersion", "1.4.2")
check(xheyPresent) { "Extract the official XheyCameraSDK-v1.3.70.9b61b4a6.Android.zip into android-camera/xhey-sdk/1.3.70/ before building. See README.md." }
val stagedAssets = layout.buildDirectory.dir("generated/xhey-assets")
val stageXheyAssets = tasks.register<Sync>("stageXheyAssets") {
    from(xheyAssets) { include("XheyCameraSDKAssets/**") }
    into(stagedAssets)
}

android {
    namespace = "cn.idi.camera"
    compileSdk = 36
    defaultConfig {
        applicationId = "cn.idi.camera"
        minSdk = 26
        targetSdk = 36
        // The supplied SDK has ARM binaries only. Do not let CameraX's x86
        // libraries select a process ABI with no matching vendor library.
        ndk { abiFilters += setOf("arm64-v8a", "armeabi-v7a") }
        versionCode = 12
        versionName = "0.5.3"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("boolean", "XHEY_SDK_PRESENT", xheyPresent.toString())
        buildConfigField("String", "XHEY_SDK_VERSION", "\"1.3.70.9b61b4a6\"")
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures { buildConfig = true }
    sourceSets.getByName("main").assets.directories.add(stagedAssets.get().asFile.path)
    val signingFile = rootProject.file("signing.properties")
    if (signingFile.isFile) {
        val signingValues = Properties().apply { signingFile.inputStream().use { load(it) } }
        signingConfigs.create("localRelease") {
            storeFile = rootProject.file(signingValues.getProperty("storeFile"))
            storePassword = signingValues.getProperty("storePassword")
            keyAlias = signingValues.getProperty("keyAlias")
            keyPassword = signingValues.getProperty("keyPassword")
        }
        buildTypes.getByName("release").signingConfig = signingConfigs.getByName("localRelease")
    }
    buildTypes.getByName("release") { isMinifyEnabled = false }
}

tasks.named("preBuild").configure { dependsOn(stageXheyAssets) }

dependencies {
    if (xheyPresent) implementation(xheyAars)
    // List the exact additional Maven dependencies required by the delivered SDK demo.
    xhey.getProperty("dependencies", "").split(',').map { it.trim() }.filter { it.isNotEmpty() }.forEach { implementation(it) }
    implementation("androidx.activity:activity:1.10.1")
    implementation("androidx.core:core:1.16.0")
    implementation("androidx.camera:camera-camera2:$cameraXVersion")
    implementation("androidx.camera:camera-lifecycle:$cameraXVersion")
    implementation("androidx.camera:camera-view:$cameraXVersion")
    implementation("androidx.exifinterface:exifinterface:1.4.1")
    implementation("androidx.work:work-runtime:2.10.1")
    implementation("androidx.webkit:webkit:1.17.1")
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:rules:1.6.1")
}
