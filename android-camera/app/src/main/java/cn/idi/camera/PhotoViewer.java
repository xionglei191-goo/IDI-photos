package cn.idi.camera;

import android.app.Dialog;
import android.content.ClipData;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Matrix;
import android.net.Uri;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.core.content.FileProvider;
import java.io.File;

final class PhotoViewer {
  static void show(MainActivity a, CaptureStore.Item item) {
    if (item.video()) { showVideo(a, item); return; }
    Ui u = a.ui;
    File photo = a.store.stamped(item.id);
    if (!photo.isFile()) photo = a.store.original(item.id);
    if (!photo.isFile()) {
      a.toast("照片文件未完整保存，请重新拍摄");
      return;
    }
    File display = photo;
    Dialog dialog = new Dialog(a, android.R.style.Theme_Material_Light_NoActionBar);
    LinearLayout page = u.column();
    page.setBackgroundColor(Ui.DARK);
    page.setPadding(u.dp(12), u.dp(32), u.dp(12), u.dp(24));
    LinearLayout header = u.row();
    header.addView(u.icon("back", "关闭照片", Color.WHITE, dialog::dismiss));
    LinearLayout labels = u.column();
    labels.addView(
        u.text(CameraData.label(item.meta.optString("photo_group")), 18, Color.WHITE, true));
    labels.addView(u.text(item.meta.optString("case_number"), 12, Color.LTGRAY, false));
    u.weight(header, labels);
    page.addView(header);
    ZoomImage image = new ZoomImage(a);
    BitmapFactory.Options bounds = new BitmapFactory.Options();
    bounds.inJustDecodeBounds = true;
    BitmapFactory.decodeFile(display.getPath(), bounds);
    BitmapFactory.Options options = new BitmapFactory.Options();
    options.inSampleSize = 1;
    while (Math.max(bounds.outWidth, bounds.outHeight) / options.inSampleSize > 3000)
      options.inSampleSize *= 2;
    Bitmap bitmap;
    try {
      bitmap = OfficialPhotoWriter.preview(display, 3000);
    } catch (Exception error) {
      a.toast("照片预览无法读取，原片仍保留");
      return;
    }
    image.setImageBitmap(bitmap);
    page.addView(image, new LinearLayout.LayoutParams(-1, 0, 1));
    TextView hint =
        u.text(
            item.official() ? RecordsScreen.label(item) + " · 双指放大" : "双指放大查看成片水印",
            13,
            Color.LTGRAY,
            false);
    hint.setGravity(Gravity.CENTER);
    page.addView(hint);
    u.gap(page, 12);
    LinearLayout buttons = u.row();
    TextView share =
        u.button(
            item.official() ? "导出 SDK 原片" : "导出水印图",
            true,
            () -> share(a, item.official() ? a.store.original(item.id) : a.store.stamped(item.id)));
    buttons.addView(share, new LinearLayout.LayoutParams(0, -2, 1));
    TextView original = u.button("导出原图", false, () -> share(a, a.store.original(item.id)));
    LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, -2, 1);
    p.leftMargin = u.dp(10);
    if (!item.official()) buttons.addView(original, p);
    page.addView(buttons);
    dialog.setContentView(page);
    dialog.setOnDismissListener(
        d -> {
          image.setImageDrawable(null);
          if (bitmap != null) bitmap.recycle();
        });
    dialog.show();
  }

  private static void showVideo(MainActivity a, CaptureStore.Item item) {
    File file = a.store.original(item.id);
    if (!file.isFile()) { a.toast("视频尚未完整保存，请在记录页重试"); return; }
    Ui u = a.ui;
    Dialog dialog = new Dialog(a, android.R.style.Theme_Material_Light_NoActionBar);
    LinearLayout page = u.column();
    page.setBackgroundColor(Ui.DARK);
    page.setPadding(u.dp(12), u.dp(32), u.dp(12), u.dp(24));
    LinearLayout header = u.row();
    header.addView(u.icon("back", "关闭视频", Color.WHITE, dialog::dismiss));
    u.weight(header, u.text("视频 · " + CameraData.label(item.meta.optString("photo_group")), 18, Color.WHITE, true));
    page.addView(header);
    android.widget.VideoView player = new android.widget.VideoView(a);
    player.setContentDescription("视频播放，可暂停和拖动进度");
    android.widget.FrameLayout frame = new android.widget.FrameLayout(a);
    frame.addView(player, new android.widget.FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));
    page.addView(frame, new LinearLayout.LayoutParams(-1, 0, 1));
    android.widget.MediaController controls = new android.widget.MediaController(a);
    controls.setAnchorView(player);
    player.setMediaController(controls);
    player.setVideoPath(file.getAbsolutePath());
    player.setOnPreparedListener(media -> { player.start(); controls.show(3000); });
    player.setOnErrorListener((media, what, extra) -> {
      a.toast("本机播放失败，视频原片仍保留，可导出查看"); return true;
    });
    page.addView(u.text(RecordsScreen.label(item), 13, Color.LTGRAY, false));
    u.gap(page, 12);
    page.addView(u.button("导出视频原片", true, () -> share(a, file)));
    dialog.setContentView(page);
    dialog.setOnDismissListener(d -> { controls.hide(); player.stopPlayback(); });
    dialog.show();
  }

  static void share(MainActivity a, File file) {
    if (!file.isFile()) {
      a.toast("文件尚未保存");
      return;
    }
    Uri uri = FileProvider.getUriForFile(a, BuildConfig.APPLICATION_ID + ".files", file);
    Intent send =
        new Intent(Intent.ACTION_SEND)
            .setType(file.getName().endsWith(".mp4") ? "video/mp4" : "image/jpeg")
            .putExtra(Intent.EXTRA_STREAM, uri)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
    send.setClipData(ClipData.newUri(a.getContentResolver(), "拍摄原片", uri));
    a.startActivity(Intent.createChooser(send, "导出原片"));
  }

  static final class ZoomImage extends ImageView {
    final Matrix transform = new Matrix();
    final ScaleGestureDetector detector;
    float x, y, scale = 1, base = 1;

    ZoomImage(android.content.Context c) {
      super(c);
      setScaleType(ImageView.ScaleType.MATRIX);
      setContentDescription("照片预览，可双指缩放");
      detector =
          new ScaleGestureDetector(
              c,
              new ScaleGestureDetector.SimpleOnScaleGestureListener() {
                @Override
                public boolean onScale(ScaleGestureDetector d) {
                  float next = Math.max(base, Math.min(base * 8, scale * d.getScaleFactor()));
                  float factor = next / scale;
                  transform.postScale(factor, factor, d.getFocusX(), d.getFocusY());
                  scale = next;
                  setImageMatrix(transform);
                  return true;
                }
              });
    }

    @Override
    protected void onSizeChanged(int w, int h, int oldw, int oldh) {
      super.onSizeChanged(w, h, oldw, oldh);
      if (getDrawable() == null) return;
      base =
          Math.min(
              w / (float) getDrawable().getIntrinsicWidth(),
              h / (float) getDrawable().getIntrinsicHeight());
      scale = base;
      transform.reset();
      transform.postScale(base, base);
      transform.postTranslate(
          (w - getDrawable().getIntrinsicWidth() * base) / 2,
          (h - getDrawable().getIntrinsicHeight() * base) / 2);
      setImageMatrix(transform);
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
      detector.onTouchEvent(e);
      if (e.getActionMasked() == MotionEvent.ACTION_DOWN) {
        x = e.getX();
        y = e.getY();
      } else if (e.getActionMasked() == MotionEvent.ACTION_MOVE && !detector.isInProgress()) {
        transform.postTranslate(e.getX() - x, e.getY() - y);
        setImageMatrix(transform);
        x = e.getX();
        y = e.getY();
      } else if (e.getActionMasked() == MotionEvent.ACTION_UP) performClick();
      return true;
    }

    @Override
    public boolean performClick() {
      super.performClick();
      return true;
    }
  }
}
