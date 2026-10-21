package cn.idi.camera;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import androidx.exifinterface.media.ExifInterface;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;

/** SDK JPEG is opaque evidence: never draw, resize, compress or rewrite its EXIF. */
final class OfficialPhotoWriter {
  static void save(CaptureStore store, String id, byte[] bytes) throws Exception {
    if (bytes.length == 0 || bytes.length >= 20 * 1024 * 1024)
      throw new Exception("SDK 原片超过支持大小，请保留原片并核对 SDK 设置");
    BitmapFactory.Options bounds = new BitmapFactory.Options();
    bounds.inJustDecodeBounds = true;
    BitmapFactory.decodeByteArray(bytes, 0, bytes.length, bounds);
    if (!"image/jpeg".equals(bounds.outMimeType)
        || bounds.outWidth <= 0
        || bounds.outHeight <= 0
        || (long) bounds.outWidth * bounds.outHeight > 60_000_000)
      throw new Exception("SDK 未返回支持的 JPEG 原片");
    File target = store.original(id), temp = new File(store.directory(id), "official.pending");
    try (FileOutputStream out = new FileOutputStream(temp)) {
      out.write(bytes);
      out.getFD().sync();
    }
    Files.move(
        temp.toPath(),
        target.toPath(),
        StandardCopyOption.REPLACE_EXISTING,
        StandardCopyOption.ATOMIC_MOVE);
    recover(store, id);
  }

  static void recover(CaptureStore store, String id) throws Exception {
    // Existing viewer uses watermark.jpg. This is a byte-identical copy, not a derivative.
    Files.copy(
        store.original(id).toPath(),
        store.stamped(id).toPath(),
        StandardCopyOption.REPLACE_EXISTING);
    Bitmap bitmap = preview(store.original(id), 640);
    if (bitmap == null) throw new Exception("SDK 原片预览不可读");
    try (FileOutputStream out = new FileOutputStream(store.thumbnail(id))) {
      if (!bitmap.compress(Bitmap.CompressFormat.JPEG, 82, out)) throw new Exception("预览保存失败");
    } finally {
      bitmap.recycle();
    }
  }

  static Bitmap preview(File file, int maxEdge) throws Exception {
    BitmapFactory.Options bounds = new BitmapFactory.Options();
    bounds.inJustDecodeBounds = true;
    BitmapFactory.decodeFile(file.getPath(), bounds);
    BitmapFactory.Options options = new BitmapFactory.Options();
    options.inSampleSize = 1;
    while (Math.max(bounds.outWidth, bounds.outHeight) / options.inSampleSize > maxEdge)
      options.inSampleSize *= 2;
    Bitmap bitmap = BitmapFactory.decodeFile(file.getPath(), options);
    if (bitmap == null) return null;
    ExifInterface exif = new ExifInterface(file);
    Matrix matrix = new Matrix();
    if (exif.isFlipped()) matrix.postScale(-1, 1);
    matrix.postRotate(exif.getRotationDegrees());
    if (matrix.isIdentity()) return bitmap;
    Bitmap rotated =
        Bitmap.createBitmap(bitmap, 0, 0, bitmap.getWidth(), bitmap.getHeight(), matrix, true);
    if (rotated != bitmap) bitmap.recycle();
    return rotated;
  }

  static String sha256(File file) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (java.io.InputStream in = Files.newInputStream(file.toPath())) {
      byte[] buffer = new byte[65536];
      int count;
      while ((count = in.read(buffer)) != -1) digest.update(buffer, 0, count);
    }
    StringBuilder text = new StringBuilder();
    for (byte b : digest.digest())
      text.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
    return text.toString();
  }
}
