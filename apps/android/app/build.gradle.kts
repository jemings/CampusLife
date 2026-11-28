plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
}

android {
    namespace = "campuslife.android"
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        applicationId = "campuslife.android"
        minSdk = libs.versions.minSdk.get().toInt()
        targetSdk = libs.versions.targetSdk.get().toInt()
        versionCode = 1
        versionName = "0.1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.toVersion(libs.versions.jvmTarget.get())
        targetCompatibility = JavaVersion.toVersion(libs.versions.jvmTarget.get())
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    testOptions {
        unitTests {
            isIncludeAndroidResources = true
        }
    }
}

ksp {
    arg("room.schemaLocation", layout.projectDirectory.dir("schemas").asFile.path)
}

val referenceDdl = layout.projectDirectory.file("../../../packages/api-contracts/ddl/client.sql")
val contractFixtures = layout.projectDirectory.dir("../../../packages/api-contracts/fixtures")

// Set by apps/android/e2e/hub_session.py around a Hub it serves (decision 4); the tests under e2e run only then.
val e2eHubUrl = providers.environmentVariable("CAMPUSLIFE_E2E_URL")
val e2eOffer = providers.environmentVariable("CAMPUSLIFE_E2E_OFFER")

tasks.withType<Test>().configureEach {
    inputs.file(referenceDdl)
        .withPropertyName("referenceDdl")
        .withPathSensitivity(PathSensitivity.NONE)
    inputs.dir(contractFixtures)
        .withPropertyName("contractFixtures")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    systemProperty("campuslife.contract.ddl", referenceDdl.asFile.canonicalPath)
    systemProperty("campuslife.contract.fixtures", contractFixtures.asFile.canonicalPath)
    if (e2eHubUrl.isPresent) {
        systemProperty("campuslife.e2e.url", e2eHubUrl.get())
        systemProperty("campuslife.e2e.offer", e2eOffer.get())
        filter.includeTestsMatching("campuslife.android.e2e.*")
    } else {
        filter.excludeTestsMatching("campuslife.android.e2e.*")
    }
}

dependencies {
    implementation(project(":wire"))
    implementation(libs.kotlinx.coroutines.core)

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.material3)
    implementation(libs.activity.compose)
    implementation(libs.room.runtime)
    ksp(libs.room.compiler)
    implementation(libs.work.runtime)
    implementation(libs.okhttp)
    implementation(libs.camera.camera2)
    implementation(libs.camera.lifecycle)
    implementation(libs.camera.compose)
    implementation(libs.zxing.core)

    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.androidx.test.ext.junit)
    testImplementation(libs.work.testing)
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.test.manifest)
}
