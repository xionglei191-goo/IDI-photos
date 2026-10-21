package cn.idi.camera;

import android.content.Context;
import androidx.annotation.NonNull;
import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.Data;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

public final class UploadWorker extends Worker {
  public UploadWorker(@NonNull Context context, @NonNull WorkerParameters parameters) {
    super(context, parameters);
  }

  static void enqueue(Context context, String id, boolean manual) {
    OneTimeWorkRequest work =
        new OneTimeWorkRequest.Builder(UploadWorker.class)
            .setInputData(new Data.Builder().putString("capture_id", id).build())
            .setConstraints(
                new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .addTag("idi-upload")
            .build();
    WorkManager.getInstance(context)
        .enqueueUniqueWork(
            "capture-" + id, manual ? ExistingWorkPolicy.REPLACE : ExistingWorkPolicy.KEEP, work);
  }

  static void pending(Context context) {
    CaptureStore store = CaptureStore.get(context);
    for (CaptureStore.Item item : store.items())
      if (!item.demo()
          && item.official()
          && (item.state.equals("queued")
              || item.state.equals("verifying")
              || item.state.equals("retry")
              || item.state.equals("uploading"))) enqueue(context, item.id, false);
  }

  @NonNull
  @Override
  public Result doWork() {
    CaptureStore store = CaptureStore.get(getApplicationContext());
    String id = getInputData().getString("capture_id");
    CaptureStore.Item item = store.item(id);
    if (item == null
        || item.demo()
        || item.state.equals("uploaded")
        || item.state.equals("cancelled")
        || item.state.equals("camera_error")
        || item.state.equals("capture_failed")) return Result.success();
    if (!item.official()) {
      store.state(id, "failed", "自绘水印不具备官方验真，请使用今日 SDK 重新拍摄");
      return Result.failure();
    }
    if (isStopped()) return Result.retry();
    store.state(id, "uploading", "");
    try {
      JSONObject receipt =
          item.receipt.isEmpty()
              ? CameraApi.upload(store, item)
              : CameraApi.request(
                  item.server,
                  "/api/camera/official-captures/" + id,
                  store.token(item.device),
                  null);
      if (!id.equals(receipt.optString("capture_id"))
          || item.video() && !"video".equals(receipt.optString("media_type"))
          || !OfficialPhotoWriter.sha256(store.original(id))
              .equals(receipt.optString("original_hash")))
        throw new java.io.IOException("官方原片回执编号或哈希不匹配，未确认归档");
      String verification = receipt.optString("verification_state");
      if (("verified".equals(verification) || "not_requested".equals(verification))
          && "uploaded".equals(receipt.optString("state"))
          && receipt.opt("photo_id") instanceof String
          && !receipt.optString("photo_id").isEmpty()) {
        store.uploaded(id, receipt);
        return Result.success();
      }
      if (verification.equals("rejected")) {
        store.receipt(id, "rejected", receipt);
        return Result.failure();
      }
      if (verification.equals("error")
          || verification.equals("cancelled")
          || verification.equals("unconfigured")) {
        if (receipt.optString("error").isEmpty()) receipt.put("error", "电脑端官方验真尚未启用，请配置后重试");
        store.receipt(id, "verification_error", receipt);
        return Result.failure();
      }
      if (!verification.equals("queued")
          && !verification.equals("submitting")
          && !verification.equals("processing")) throw new java.io.IOException("官方验真回执状态未知，未确认归档");
      store.receipt(id, "verifying", receipt);
      if (getRunAttemptCount() >= 8) {
        store.state(id, "failed", "仍在等待官方结果，可稍后继续查询");
        return Result.failure();
      }
      return Result.retry();
    } catch (CameraApi.Failure error) {
      boolean retry = error.retryable() && getRunAttemptCount() < 5;
      store.state(id, retry ? "retry" : "failed", error.getMessage());
      return retry ? Result.retry() : Result.failure();
    } catch (Exception error) {
      boolean retry = getRunAttemptCount() < 5;
      store.state(id, retry ? "retry" : "failed", "连接中断，原片仍在手机：" + safeMessage(error));
      return retry ? Result.retry() : Result.failure();
    }
  }

  static String safeMessage(Throwable error) {
    String message = error.getMessage();
    return message == null || message.trim().isEmpty()
        ? "请检查网络或稍后重试"
        : message.substring(0, Math.min(240, message.length()));
  }
}
