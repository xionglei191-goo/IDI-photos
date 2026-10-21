package cn.idi.camera;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import com.xhey.xheycamerasdk.CameraActivity;
import com.xhey.xheycamerasdk.CameraConfigMode;
import com.xhey.xheycamerasdk.CameraSDKVersion;
import com.xhey.xheycamerasdk.CameraViewConfig;
import com.xhey.xheycamerasdk.CapturedImage;
import com.xhey.xheycamerasdk.Logger;
import com.xhey.xheycamerasdk.UserComment;
import com.xhey.xheycamerasdk.camera.CameraBackendType;
import com.xhey.xheycamerasdk.camera.CameraCaptureMode;
import com.xhey.xheycamerasdk.camera.VideoResolution;
import com.xhey.xheycamerasdk.recorder.VideoInfo;
import java.util.HashMap;
import org.json.JSONObject;

/** Typed contract for the supplied, unmodified 1.3.70.9b61b4a6 Android SDK. */
final class ConfiguredXheyAdapter {
  static final String CAPTURE_ID = "idi_capture_id";
  static final String[] FIELDS = {"案件号", "小区", "位置", "环节", "内容"};

  static String unavailableReason() {
    if (!BuildConfig.XHEY_SDK_PRESENT || !BuildConfig.XHEY_SDK_VERSION.equals(CameraSDKVersion.VERSION))
      return "官方 SDK 版本不匹配，请重新安装";
    for (String abi : Build.SUPPORTED_ABIS)
      if (abi.equals("arm64-v8a") || abi.equals("armeabi-v7a")) return "";
    return "本版今日原生 SDK 仅支持 ARM 安卓设备，请在安卓手机上使用";
  }

  static void silenceVendorLogs() {
    // Config toString() includes the signed session. Never forward vendor logs.
    Logger.setDelegate(new Logger.LoggerDelegate() {
      @Override public void logInfo(String tag, String message) {}
      @Override public void logError(String tag, String message) {}
      @Override public void logWarn(String tag, String message) {}
    });
  }

  static Intent intent(Activity activity, JSONObject session, String captureId, boolean emulatorTest)
      throws Exception {
    if (!captureId.equals(session.getString("capture_id"))
        || !BuildConfig.XHEY_SDK_VERSION.equals(session.getString("sdk_version")))
      throw new Exception("拍摄授权与本次拍摄不匹配");
    JSONObject config = session.getJSONObject("options");
    JSONObject input = config.getJSONObject("customInputItems");
    HashMap<String, String> fields = new HashMap<>();
    for (String field : FIELDS) {
      String value = input.getString(field);
      if (value.trim().isEmpty()) throw new Exception("案件水印字段不完整");
      fields.put(field, value);
    }
    Intent intent = new Intent(activity, EditableCameraActivity.class);
    intent.putExtra(CAPTURE_ID, captureId);
    intent.putExtra(CameraViewConfig.kAppid, config.getString("appid"));
    intent.putExtra(CameraViewConfig.kNoncestr, config.getString("noncestr"));
    intent.putExtra(CameraViewConfig.kTimestamp, config.getInt("timestamp"));
    intent.putExtra(CameraViewConfig.kSignature, config.getString("signature"));
    // kSecretKey is deliberately absent. This version accepts server-generated signatures.
    intent.putExtra(CameraViewConfig.kMaxImageCount, 1);
    intent.putExtra(CameraViewConfig.kNeedPhotoConfirm, true);
    boolean video = "video".equals(session.optString("media_type"));
    intent.putExtra(EditableCameraActivity.VIDEO_ENABLED, session.has("max_record_seconds"));
    intent.putExtra(CameraViewConfig.kCameraMode,
        (video ? CameraConfigMode.Video : CameraConfigMode.Photo).getValue());
    if (video) {
      intent.putExtra(CameraViewConfig.kMaxRecordDuration, 60);
      intent.putExtra(CameraViewConfig.kVideoResolution, VideoResolution.P720.getValue());
      intent.putExtra(CameraViewConfig.kVideoBitrate, 4_000_000);
      intent.putExtra(CameraViewConfig.kVideoSaveDir, CaptureStore.get(activity).directory(captureId).getAbsolutePath());
    }
    intent.putExtra(CameraViewConfig.kCameraBackendType, CameraBackendType.CAMERAX.getValue());
    intent.putExtra(CameraViewConfig.kCaptureMode, CameraCaptureMode.QUALITY_PRIORITY.getValue());
    intent.putExtra(CameraViewConfig.kResourceDir, "file:///android_asset/idi-native-camera");
    if (!config.optString("groupWatermarkId").isEmpty())
      intent.putExtra(CameraViewConfig.kGroupWatermarkId, config.getString("groupWatermarkId"));
    // The SDK constructor requires a Serializable HashMap, not a JSON string.
    intent.putExtra(CameraViewConfig.kCustomInputItems, fields);
    intent.putExtra(CameraViewConfig.kInterceptOnEmulator, !(BuildConfig.DEBUG && emulatorTest));
    return intent;
  }

  static CapturedImage photo(CameraActivity.Result result) throws Exception {
    if (result == null || !result.success || result.error != null
        || result.capturedImages == null || result.capturedImages.size() != 1)
      throw new Exception("官方相机未返回一张完整照片，请重新拍摄");
    CapturedImage image = result.capturedImages.get(0);
    if (image == null || image.getImageData() == null || image.getImageData().length == 0)
      throw new Exception("官方相机未返回原片数据");
    return image;
  }

  static String comment(CapturedImage image) {
    UserComment comment = image.getUserComment();
    if (comment == null) return "{}";
    String raw = comment.getRawJson();
    if (raw == null || raw.length() > 65536) return "{}";
    try { new JSONObject(raw); return raw; }
    catch (Exception error) { return "{}"; }
  }

  static VideoInfo video(CameraActivity.RecordingResult result) throws Exception {
    if (result == null || !result.success || result.error != null || result.videoInfo == null
        || result.videoInfo.filePath == null || result.videoInfo.filePath.isEmpty())
      throw new Exception("录像未返回完整视频，请返回记录页核对");
    return result.videoInfo;
  }
}
