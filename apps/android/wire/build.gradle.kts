import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlin.jvm)
}

val bytecodeTarget: String = libs.versions.jvmTarget.get()

java {
    sourceCompatibility = JavaVersion.toVersion(bytecodeTarget)
    targetCompatibility = JavaVersion.toVersion(bytecodeTarget)
}

kotlin {
    compilerOptions {
        jvmTarget = JvmTarget.fromTarget(bytecodeTarget)
    }
}

dependencies {
    testImplementation(libs.junit)
}

tasks.test {
    val fixtures = layout.projectDirectory.dir("../../../packages/api-contracts/fixtures")
    inputs.dir(fixtures)
        .withPropertyName("contractFixtures")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    systemProperty("campuslife.contract.fixtures", fixtures.asFile.canonicalPath)
}
