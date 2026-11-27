import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    `java-library`
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

val jdkRelease: String = libs.versions.wireJdkRelease.get()

java {
    sourceCompatibility = JavaVersion.toVersion(jdkRelease)
    targetCompatibility = JavaVersion.toVersion(jdkRelease)
}

kotlin {
    compilerOptions {
        jvmTarget = JvmTarget.fromTarget(jdkRelease)
        freeCompilerArgs.add("-Xjdk-release=$jdkRelease")
    }
}

tasks.withType<JavaCompile>().configureEach {
    options.release = JavaVersion.toVersion(jdkRelease).majorVersion.toInt()
}

dependencies {
    api(libs.kotlinx.serialization.json)
    testImplementation(libs.junit)
}

val contractPackage = layout.projectDirectory.dir("../../../packages/api-contracts")
val contractFixtures = contractPackage.dir("fixtures")
val contractSchemas = contractPackage.dir("schemas")
val contractInit = contractPackage.file("src/campuslife_contracts/__init__.py")

tasks.test {
    inputs.dir(contractFixtures)
        .withPropertyName("contractFixtures")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.dir(contractSchemas)
        .withPropertyName("contractSchemas")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.file(contractInit)
        .withPropertyName("contractInit")
        .withPathSensitivity(PathSensitivity.NONE)
    systemProperty("campuslife.contract.fixtures", contractFixtures.asFile.canonicalPath)
    systemProperty("campuslife.contract.schemas", contractSchemas.asFile.canonicalPath)
    systemProperty("campuslife.contract.init", contractInit.asFile.canonicalPath)
}

tasks.register<JavaExec>("contractHarness") {
    description = "Round-trips every contract fixture into build/contract-harness (packages/api-contracts/HARNESS.md)."
    group = "verification"
    val out = layout.buildDirectory.dir("contract-harness")
    inputs.dir(contractFixtures)
        .withPropertyName("contractFixtures")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    outputs.dir(out)
        .withPropertyName("out")
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "campuslife.wire.harness.HarnessKt"
    args(contractFixtures.asFile.canonicalPath, out.get().asFile.path)
}
