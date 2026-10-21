package cn.idi.camera;

import android.graphics.Bitmap;
import android.media.MediaMetadataRetriever;
import com.xhey.xheycamerasdk.recorder.VideoInfo;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;

/** Retain the SDK's MP4 bytes; the separately generated poster is only a preview. */
final class OfficialVideoWriter {
  static final long MAX_BYTES = 64L * 1024 * 1024;

  static boolean hasCandidate(CaptureStore store, String id) {
    File[] files = store.directory(id).listFiles(file ->
        file.isFile() && file.getName().toLowerCase(java.util.Locale.ROOT).endsWith(".mp4"));
    return files != null && files.length > 0;
  }

  static void save(CaptureStore store, String id, VideoInfo video) throws Exception {
    File source = new File(video.filePath).getCanonicalFile();
    if (!source.getParentFile().equals(store.directory(id).getCanonicalFile()))
      throw new Exception("录像文件不属于本次拍摄，未归档");
    retain(store, id, source);
    recover(store, id);
  }

  private static void retain(CaptureStore store, String id, File source) throws Exception {
    if (!source.isFile() || source.length() == 0 || source.length() >= MAX_BYTES)
      throw new Exception("视频为空或超过64 MiB，已有文件仍保留");
    try (MediaMetadataRetriever media = new MediaMetadataRetriever()) {
      media.setDataSource(source.getPath());
      validate(media);
    }
    File target = store.original(id);
    if (!source.getCanonicalFile().equals(target.getCanonicalFile())) {
      if (target.exists()) {
        if (!OfficialPhotoWriter.sha256(source).equals(OfficialPhotoWriter.sha256(target)))
          throw new Exception("本次拍摄已有不同原片，未覆盖");
      } else {
        Files.move(source.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE);
      }
    }
  }

  static void recover(CaptureStore store, String id) throws Exception {
    File target = store.original(id);
    if (!target.isFile()) {
      File[] candidates = store.directory(id).listFiles(file ->
          file.isFile() && file.getName().toLowerCase(java.util.Locale.ROOT).endsWith(".mp4"));
      if (candidates == null || candidates.length != 1)
        throw new Exception("未找到唯一的完整录像，已有文件仍保留");
      retain(store, id, candidates[0]);
    }
    Bitmap frame = null;
    try (MediaMetadataRetriever media = new MediaMetadataRetriever()) {
      media.setDataSource(target.getPath());
      validate(media);
      int width = Integer.parseInt(media.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH));
      int height = Integer.parseInt(media.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT));
      float scale = Math.min(1f, 640f / Math.max(width, height));
      if (android.os.Build.VERSION.SDK_INT >= 27) {
        frame = media.getScaledFrameAtTime(0, MediaMetadataRetriever.OPTION_CLOSEST_SYNC,
            Math.max(1, (int) (width * scale)), Math.max(1, (int) (height * scale)));
      } else {
        frame = media.getFrameAtTime(0, MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
        if (frame != null && scale < 1) {
          Bitmap scaled = Bitmap.createScaledBitmap(frame,
              Math.max(1, (int) (width * scale)), Math.max(1, (int) (height * scale)), true);
          if (scaled != frame) frame.recycle();
          frame = scaled;
        }
      }
      if (frame == null) throw new Exception("视频预览无法解码，原片已保留");
      File pending = new File(store.directory(id), "thumbnail.pending");
      try (FileOutputStream out = new FileOutputStream(pending)) {
        if (!frame.compress(Bitmap.CompressFormat.JPEG, 82, out)) throw new Exception("视频预览保存失败");
        out.getFD().sync();
      }
      Files.move(pending.toPath(), store.thumbnail(id).toPath(),
          StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    } finally {
      if (frame != null) frame.recycle();
    }
  }

  private static void validate(MediaMetadataRetriever media) throws Exception {
    long duration = Long.parseLong(media.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION));
    String mime = media.extractMetadata(MediaMetadataRetriever.METADATA_KEY_MIMETYPE);
    if (!"yes".equals(media.extractMetadata(MediaMetadataRetriever.METADATA_KEY_HAS_VIDEO))
        || !"video/mp4".equals(mime) || duration <= 0 || duration > 65_000)
      throw new Exception("视频未完整保存或超过60秒录制范围，原片仍保留");
  }
}
