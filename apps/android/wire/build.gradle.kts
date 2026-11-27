import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    `java-library`
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.android.lint)
}

val bytecodeTarget: String = libs.versions.jvmTarget.get()

java {
    sourceCompatibility = JavaVersion.toVersion(bytecodeTarget)
    targetCompatibility = JavaVersion.toVersion(bytecodeTarget)
}

kotlin {
    compilerOptions {
        jvmTarget = JvmTarget.fromTarget(bytecodeTarget)
        freeCompilerArgs.add("-Xjdk-release=$bytecodeTarget")
    }
}

tasks.withType<JavaCompile>().configureEach {
    options.release = bytecodeTarget.toInt()
}

dependencies {
    api(libs.kotlinx.serialization.json)
    testImplementation(libs.junit)
}

val contractPackage = layout.projectDirectory.dir("../../../packages/api-contracts")
val contractFixtures = contractPackage.dir("fixtures")
val contractInit = contractPackage.file("src/campuslife_contracts/__init__.py")

tasks.test {
    inputs.dir(contractFixtures)
        .withPropertyName("contractFixtures")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.file(contractInit)
        .withPropertyName("contractInit")
        .withPathSensitivity(PathSensitivity.NONE)
    systemProperty("campuslife.contract.fixtures", contractFixtures.asFile.canonicalPath)
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
