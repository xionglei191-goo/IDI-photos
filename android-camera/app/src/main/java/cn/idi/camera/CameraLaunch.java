package cn.idi.camera;

/** Local launch preparation must not queue behind network sync or image recovery. */
final class CameraLaunch {
  private CameraLaunch() {}
  static final java.util.concurrent.ExecutorService io =
      java.util.concurrent.Executors.newSingleThreadExecutor(r -> new Thread(r, "idi-camera-launch"));
}
