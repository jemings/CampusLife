package campuslife.wire;

final class JdkReleaseProbe {
    static int probe() {
        return Math.clamp(5L, 0, 10);
    }
}
