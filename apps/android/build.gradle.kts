import org.gradle.api.tasks.testing.logging.TestExceptionFormat
import org.gradle.api.tasks.testing.logging.TestLogEvent

plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.ksp) apply false
}

subprojects {
    tasks.withType<Test>().configureEach {
        testLogging {
            events(TestLogEvent.FAILED)
            exceptionFormat = TestExceptionFormat.FULL
        }
        val taskPath = path
        afterSuite(
            KotlinClosure2<TestDescriptor, TestResult, Unit>({ suite, result ->
                if (suite.parent == null) {
                    println(
                        "$taskPath: ${result.testCount} tests, ${result.failedTestCount} failed, " +
                            "${result.skippedTestCount} skipped",
                    )
                }
            }),
        )
    }
}
