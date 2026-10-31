package cn.idi.camera;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Rect;
import android.view.Gravity;
import android.view.PixelCopy;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.TextView;
import java.lang.ref.WeakReference;

/** An in-memory preview cover while the SDK releases and reconfigures its camera. */
final class CameraPreviewTransition {
  private static WeakReference<NativeCameraActivity> coordinator = new WeakReference<>(null);

  static void attach(NativeCameraActivity activity) { coordinator = new WeakReference<>(activity); }

  static void detach(NativeCameraActivity activity) {
    if (coordinator.get() == activity) coordinator.clear();
  }

  static Bitmap frame(String captureId) {
    NativeCameraActivity activity = coordinator.get();
    return activity != null && captureId.equals(activity.captureId) ? activity.transitionFrame : null;
  }

  static void clear(String captureId) {
    NativeCameraActivity activity = coordinator.get();
    if (activity != null && captureId.equals(activity.captureId)) activity.clearTransition();
  }

  static void capture(EditableCameraActivity source, Runnable ready, Runnable failed) {
    NativeCameraActivity activity = coordinator.get();
    View decor = source.getWindow().getDecorView();
    SurfaceView surface = surface(decor);
    if (activity == null || activity.isFinishing() || !source.captureId.equals(activity.captureId)
        || decor.getWidth() == 0 || decor.getHeight() == 0 || source.renderer == null
        || surface == null || !surface.getHolder().getSurface().isValid()) { failed.run(); return; }
    // Bound memory while keeping enough detail to bridge a short mode change.
    int width = Math.min(720, decor.getWidth());
    float scale = width / (float) decor.getWidth();
    final Bitmap pixels;
    try { pixels = Bitmap.createBitmap(Math.max(1, (int) (surface.getWidth() * scale)),
        Math.max(1, (int) (surface.getHeight() * scale)), Bitmap.Config.ARGB_8888); }
    catch (RuntimeException | OutOfMemoryError error) { failed.run(); return; }
    PixelCopy.request(surface, pixels, result -> {
      try {
        if (result != PixelCopy.SUCCESS || source.isFinishing() || source.isDestroyed()
            || activity.isFinishing() || activity.isDestroyed()) { failed.run(); return; }
        Bitmap frame = Bitmap.createBitmap(width, Math.max(1, (int) (decor.getHeight() * scale)), Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(frame);
        boolean video = source.getIntent().getIntExtra(com.xhey.xheycamerasdk.CameraViewConfig.kCameraMode, 0)
            == com.xhey.xheycamerasdk.CameraConfigMode.Video.getValue();
        canvas.drawColor(video ? Color.BLACK : Color.WHITE);
        canvas.scale(scale, scale);
        int[] origin = new int[2], position = new int[2];
        decor.getLocationOnScreen(origin); surface.getLocationOnScreen(position);
        canvas.drawBitmap(pixels, null, new Rect(position[0] - origin[0], position[1] - origin[1],
            position[0] - origin[0] + surface.getWidth(), position[1] - origin[1] + surface.getHeight()), null);
        drawLayer(canvas, source.renderer, origin);
        drawLayer(canvas, source.overlay, origin);
        activity.showTransition(frame);
        // Commit the cover's first frame before allowing the old SDK window to close.
        activity.root.postOnAnimation(() -> activity.root.postOnAnimation(ready));
      } catch (RuntimeException | OutOfMemoryError error) { failed.run(); }
      finally { pixels.recycle(); }
    }, source.handler);
  }

  private static void drawLayer(Canvas canvas, View view, int[] origin) {
    int[] location = new int[2]; view.getLocationOnScreen(location);
    canvas.save(); canvas.translate(location[0] - origin[0], location[1] - origin[1]);
    view.draw(canvas); canvas.restore();
  }

  static FrameLayout cover(android.content.Context context, Bitmap bitmap, String label) {
    FrameLayout result = new FrameLayout(context);
    result.setBackgroundColor(Color.BLACK);
    result.setClickable(true);
    result.setContentDescription("相机模式切换中");
    ImageView image = new ImageView(context);
    image.setScaleType(ImageView.ScaleType.FIT_XY);
    image.setImageBitmap(bitmap);
    result.addView(image, new FrameLayout.LayoutParams(-1, -1));
    Ui ui = new Ui(context);
    TextView progress = ui.text(label, 15, Ui.MUTED, false);
    ui.pad(progress, 16); ui.surface(progress, Color.WHITE, Ui.BORDER, 12);
    FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.CENTER_HORIZONTAL);
    params.topMargin = ui.dp(52);
    result.addView(progress, params);
    return result;
  }

  static SurfaceView surface(View view) {
    if (view instanceof SurfaceView surface) return surface;
    if (view instanceof ViewGroup group) for (int i = 0; i < group.getChildCount(); i++) {
      SurfaceView result = surface(group.getChildAt(i)); if (result != null) return result;
    }
    return null;
  }
}
