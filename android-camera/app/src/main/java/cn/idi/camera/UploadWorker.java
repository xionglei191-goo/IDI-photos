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
  private static final java.util.Set<String> active = java.util.concurrent.ConcurrentHashMap.newKeySet();
  private volatile CameraApi.Transfer transfer;
  private volatile UploadStatus status;
  public UploadWorker(@NonNull Context context, @NonNull WorkerParameters parameters) {
    super(context, parameters);
  }

  static void enqueue(Context context, String id, boolean manual) {
    CaptureStore.Item item = CaptureStore.get(context).item(id);
    if (item == null || item.deleting()) return;
    OneTimeWorkRequest work =
        new OneTimeWorkRequest.Builder(UploadWorker.class)
            .setInputData(new Data.Builder().putString("capture_id", id).build())
            .setConstraints(
                new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.LINEAR, 15, TimeUnit.SECONDS)
            .addTag("idi-upload")
            .build();
    WorkManager.getInstance(context)
        .enqueueUniqueWork(
            "capture-" + id, manual ? ExistingWorkPolicy.REPLACE : ExistingWorkPolicy.KEEP, work);
  }

  static boolean active(String id) { return active.contains(id); }

  static void cancel(Context context, String id) {
    CaptureDeletion.cancelTransfers(id);
    WorkManager.getInstance(context).cancelUniqueWork("capture-" + id);
  }

  static void networkRecovered(Context context) {
    CaptureDeletion.pending(context);
    CaptureStore store = CaptureStore.get(context);
    for (CaptureStore.Item item : store.items()) {
      if (item.demo() || !item.official() || active(item.id)) continue;
      if (item.state.equals("retry") && !"HTTP_429".equals(item.upload.optString("error_code")))
        enqueue(context, item.id, true);
      else if (item.state.equals("queued") || item.state.equals("uploading") || item.state.equals("verifying"))
        enqueue(context, item.id, false);
    }
  }

  static void pending(Context context) {
    CaptureDeletion.pending(context);
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
        || item.deleting()
        || item.demo()
        || item.state.equals("uploaded")
        || item.state.equals("cancelled")
        || item.state.equals("camera_error")
        || item.state.equals("capture_failed")) return Result.success();
    if (!item.official()) {
      store.state(id, "failed", "自绘水印不具备官方验真，请使用今日 SDK 重新拍摄");
      return Result.failure();
    }
    if (isStopped() || !active.add(id)) return Result.retry();
    String owner = getId().toString();
    try {
      if (!store.claimUpload(id, owner)) return Result.success();
      status = new UploadStatus(store, item, owner);
      transfer = new CameraApi.Transfer(item.video());
      if (isStopped()) transfer.cancel();
      UploadNotification notification = new UploadNotification(getApplicationContext(), id);
      try {
        setForegroundAsync(notification.foreground(status.info)).get(5, TimeUnit.SECONDS);
        status.foreground(true);
        status.changed = () -> notification.update(status.info);
      } catch (Exception foregroundUnavailable) {
        // Background-start restrictions or denied notifications must not discard a photo.
        status.foreground(false);
      }
      JSONObject receipt = UploadEngine.transfer(store, item, transfer, status);
      String verification = receipt.optString("verification_state");
      if (("verified".equals(verification) || "not_requested".equals(verification))
          && "uploaded".equals(receipt.optString("state"))
          && receipt.opt("photo_id") instanceof String
          && !receipt.optString("photo_id").isEmpty()) {
        status.finish("uploaded", "", "", receipt, 0);
        return Result.success();
      }
      if (verification.equals("rejected")) {
        status.finish("rejected", "VERIFICATION_REJECTED", receipt.optString("error"), receipt, 0);
        return Result.failure();
      }
      if (verification.equals("error")
          || verification.equals("cancelled")
          || verification.equals("unconfigured")) {
        if (receipt.optString("error").isEmpty()) receipt.put("error", "电脑端官方验真尚未启用，请配置后重试");
        status.finish("verification_error", "VERIFICATION_ERROR", receipt.optString("error"), receipt, 0);
        return Result.failure();
      }
      if (!verification.equals("queued")
          && !verification.equals("submitting")
          && !verification.equals("processing")) throw new UploadEngine.ReceiptError("官方验真回执状态未知");
      status.finish("verifying", "", "", receipt, retryDelay());
      return Result.retry();
    } catch (Exception error) {
      boolean retry = isStopped() || UploadStatus.retryable(error);
      if (status != null) try {
        status.finish(retry ? "retry" : "failed", isStopped() ? "TASK_PAUSED" : UploadStatus.code(error),
            isStopped() ? "后台任务已暂停，网络可用时自动继续" : UploadStatus.explain(error), null, retry ? retryDelay() : 0);
      } catch (Exception ignored) { }
      return retry ? Result.retry() : Result.failure();
    } finally {
      if (transfer != null) transfer.close();
      active.remove(id);
    }
  }

  private long retryDelay() { return Math.min(18000000L, 15000L * (getRunAttemptCount() + 1)); }

  @Override public void onStopped() {
    if (transfer != null) transfer.cancel();
    // The IO call observes cancellation and records retry. WorkManager persists rescheduling.
    super.onStopped();
  }

  static String safeMessage(Throwable error) {
    String message = error.getMessage();
    return message == null || message.trim().isEmpty()
        ? "请检查网络或稍后重试"
        : message.substring(0, Math.min(240, message.length()));
  }
}
