package cn.idi.camera;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Typeface;
import androidx.exifinterface.media.ExifInterface;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONObject;

final class WatermarkRenderer {
  static WatermarkLayout render(File original, File output, File thumbnail, JSONObject metadata)
      throws IOException {
    BitmapFactory.Options bounds = new BitmapFactory.Options();
    bounds.inJustDecodeBounds = true;
    BitmapFactory.decodeFile(original.getPath(), bounds);
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) throw new IOException("原图未完整保存，请重新拍摄");
    BitmapFactory.Options options = new BitmapFactory.Options();
    options.inSampleSize = 1;
    options.inMutable = true;
    while (Math.max(bounds.outWidth, bounds.outHeight) / options.inSampleSize > 4096)
      options.inSampleSize *= 2;
    Bitmap image = BitmapFactory.decodeFile(original.getPath(), options);
    if (image == null) throw new IOException("无法读取原图");
    try {
      ExifInterface originalExif = new ExifInterface(original);
      Matrix transform = new Matrix();
      if (originalExif.isFlipped()) transform.postScale(-1, 1);
      transform.postRotate(originalExif.getRotationDegrees());
      if (!transform.isIdentity()) {
        Bitmap oriented =
            Bitmap.createBitmap(image, 0, 0, image.getWidth(), image.getHeight(), transform, true);
        if (oriented != image) {
          image.recycle();
          image = oriented;
        }
      }
      if (!image.isMutable()) {
        Bitmap editable = image.copy(Bitmap.Config.ARGB_8888, true);
        image.recycle();
        image = editable;
      }
      Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.SUBPIXEL_TEXT_FLAG);
      paint.setTextSize(100);
      List<String> lines = new ArrayList<>();
      String[] fields = CameraData.watermark(metadata);
      for (int i = 0; i < fields.length; i++) {
        String field = fields[i].replaceAll("[\\r\\n\\t]+", " ");
        if (i < 2) lines.add(field);
        else {
          int[] points = field.codePoints().toArray();
          for (int offset = 0; offset < points.length; offset += 24)
            lines.add(new String(points, offset, Math.min(24, points.length - offset)));
        }
      }
      float widestEm = 1;
      for (int i = 0; i < lines.size(); i++) {
        paint.setTypeface(
            i == 0
                ? Typeface.create("sans-serif", Typeface.BOLD)
                : Typeface.create("sans-serif", Typeface.NORMAL));
        widestEm = Math.max(widestEm, paint.measureText(lines.get(i)) / 100f);
      }
      WatermarkLayout layout =
          new WatermarkLayout(image.getWidth(), image.getHeight(), widestEm + .3f, lines.size());
      Canvas canvas = new Canvas(image);
      paint.setColor(Color.argb(174, 18, 25, 31));
      canvas.drawRoundRect(
          new RectF(layout.x, layout.y, layout.x + layout.width, layout.y + layout.height),
          layout.font * .3f,
          layout.font * .3f,
          paint);
      paint.setColor(Color.rgb(238, 89, 36));
      float barX = layout.x + layout.font * .24f;
      canvas.drawRoundRect(
          new RectF(
              barX,
              layout.y + layout.padding,
              barX + layout.font * .12f,
              layout.y + layout.height - layout.padding),
          layout.font * .06f,
          layout.font * .06f,
          paint);
      paint.setColor(Color.WHITE);
      paint.setTextSize(layout.font);
      for (int i = 0; i < lines.size(); i++) {
        paint.setTypeface(
            i == 0
                ? Typeface.create("sans-serif", Typeface.BOLD)
                : Typeface.create("sans-serif", Typeface.NORMAL));
        float baseline = layout.y + layout.padding + layout.lineHeight * i - paint.ascent();
        canvas.drawText(
            lines.get(i), layout.x + layout.padding + layout.font * .15f, baseline, paint);
      }
      write(image, output, 93);
      ExifInterface outputExif = new ExifInterface(output);
      outputExif.setAttribute(
          ExifInterface.TAG_ORIENTATION, String.valueOf(ExifInterface.ORIENTATION_NORMAL));
      outputExif.setAttribute(
          ExifInterface.TAG_DATETIME_ORIGINAL,
          OffsetDateTime.parse(metadata.optString("captured_at"))
              .format(DateTimeFormatter.ofPattern("yyyy:MM:dd HH:mm:ss")));
      outputExif.setAttribute(ExifInterface.TAG_OFFSET_TIME_ORIGINAL, "+08:00");
      outputExif.setAttribute(ExifInterface.TAG_SOFTWARE, "IDI Camera " + BuildConfig.VERSION_NAME);
      outputExif.saveAttributes();
      float scale = Math.min(1f, 640f / Math.max(image.getWidth(), image.getHeight()));
      Bitmap small =
          Bitmap.createScaledBitmap(
              image,
              Math.max(1, (int) (image.getWidth() * scale)),
              Math.max(1, (int) (image.getHeight() * scale)),
              true);
      write(small, thumbnail, 85);
      if (small != image) small.recycle();
      return layout;
    } finally {
      if (!image.isRecycled()) image.recycle();
    }
  }

  private static void write(Bitmap bitmap, File file, int quality) throws IOException {
    File temp = new File(file.getParentFile(), file.getName() + ".tmp");
    try {
      try (FileOutputStream stream = new FileOutputStream(temp)) {
        if (!bitmap.compress(Bitmap.CompressFormat.JPEG, quality, stream))
          throw new IOException("照片保存失败");
        stream.flush();
        stream.getFD().sync();
      }
      Files.move(temp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING);
    } finally {
      if (temp.exists()) temp.delete();
    }
  }
}
