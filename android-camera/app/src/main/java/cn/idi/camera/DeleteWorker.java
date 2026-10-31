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

public final class DeleteWorker extends Worker {
  private static final java.util.Set<String> active = java.util.concurrent.ConcurrentHashMap.newKeySet();
  private volatile CameraApi.Transfer transfer;

  public DeleteWorker(@NonNull Context context, @NonNull WorkerParameters parameters) {
    super(context, parameters);
  }

  static void enqueue(Context context, String id, boolean manual) {
    CaptureStore.Item item = CaptureStore.get(context).item(id);
    if (item == null || !item.state.equals("delete_pending")) return;
    OneTimeWorkRequest.Builder builder = new OneTimeWorkRequest.Builder(DeleteWorker.class)
        .setInputData(new Data.Builder().putString("capture_id", id).build())
        .setBackoffCriteria(BackoffPolicy.LINEAR, 15, TimeUnit.SECONDS)
        .addTag("idi-capture-delete");
    if (!item.demo()) builder.setConstraints(new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build());
    WorkManager.getInstance(context).enqueueUniqueWork("capture-delete-" + id,
        manual ? ExistingWorkPolicy.REPLACE : ExistingWorkPolicy.KEEP, builder.build());
  }

  @NonNull @Override public Result doWork() {
    CaptureStore store = CaptureStore.get(getApplicationContext());
    String id = getInputData().getString("capture_id");
    if (id == null) return Result.failure();
    CaptureStore.Item item = store.item(id);
    if (item == null || !item.state.equals("delete_pending")) return Result.success();
    if (isStopped() || !active.add(id)) return Result.retry();
    String owner = "delete:" + getId();
    try {
      if (!store.claimDeletion(id, owner)) return Result.success();
      transfer = new CameraApi.Transfer(false);
      if (isStopped()) transfer.cancel();
      CaptureDeletion.perform(store, item, transfer, owner);
      return Result.success();
    } catch (Exception error) {
      boolean retry = isStopped() || CaptureDeletion.retryable(error);
      store.deletionUpdate(id, owner, retry ? "delete_pending" : "delete_failed",
          isStopped() ? "删除任务已暂停，网络可用时自动继续" : CaptureDeletion.explain(error), null);
      return retry ? Result.retry() : Result.failure();
    } finally {
      if (transfer != null) transfer.close();
      active.remove(id);
    }
  }

  @Override public void onStopped() {
    if (transfer != null) transfer.cancel();
    super.onStopped();
  }
}
