package cn.idi.camera;

import android.content.Context;
import androidx.work.WorkManager;
import java.io.File;
import java.io.IOException;
import java.io.InterruptedIOException;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import org.json.JSONObject;

/** Durable deletion intent is saved before stopping transfers or contacting the server. */
final class CaptureDeletion {
  private static final ConcurrentHashMap<String, Set<CameraApi.Transfer>> transfers = new ConcurrentHashMap<>();

  static final class LocalDeleteError extends IOException {
    LocalDeleteError(String message) { super(message); }
  }

  static final class ScopeChanged extends IOException {
    ScopeChanged() { super("当前连接已改变，已停止删除处理；请使用原配对或在工作台核对"); }
  }

  static boolean currentScope(CaptureStore store, CaptureStore.Item item) {
    return item.demo() || item.server.equals(store.server()) && item.device.equals(store.device());
  }

  static void requireCurrentScope(CaptureStore store, CaptureStore.Item item) throws ScopeChanged {
    if (!currentScope(store, item)) throw new ScopeChanged();
  }

  static void track(CaptureStore store, String id, CameraApi.Transfer transfer) throws IOException {
    transfers.compute(id, (ignored, active) -> {
      if (active == null) active = ConcurrentHashMap.newKeySet();
      active.add(transfer);
      return active;
    });
    CaptureStore.Item current = store.item(id);
    if (current == null || current.deleting()) {
      transfer.cancel();
      throw new InterruptedIOException("资料已进入删除流程，不再上传");
    }
    transfer.check();
  }

  static void untrack(String id, CameraApi.Transfer transfer) {
    transfers.computeIfPresent(id, (ignored, active) -> {
      active.remove(transfer);
      return active.isEmpty() ? null : active;
    });
  }

  static void cancelTransfers(String id) {
    Set<CameraApi.Transfer> active = transfers.get(id);
    if (active != null) for (CameraApi.Transfer transfer : active) transfer.cancel();
  }

  /** Call from the app's IO executor. The return does not mean remote deletion is complete. */
  static void request(Context context, String id) throws Exception {
    CaptureStore store = CaptureStore.get(context);
    CaptureStore.Item item = store.item(id);
    if (item == null) throw new Exception("未找到这份资料，请刷新记录");
    if (item.state.equals("deleted")) return;
    requireCurrentScope(store, item);
    if (store.activeCaptures.contains(id) || java.util.Arrays.asList("capturing", "rendering", "awaiting_sdk").contains(item.state))
      throw new Exception("资料正在拍摄或保存，请完成后再删除");
    store.beginDeletion(id);
    UploadWorker.cancel(context, id);
    DeleteWorker.enqueue(context, id, false);
  }

  static void retry(Context context, String id) throws Exception {
    CaptureStore store = CaptureStore.get(context);
    CaptureStore.Item item = store.item(id);
    if (item == null || !item.deleting()) throw new Exception("这份资料没有待处理的删除请求");
    if (item.state.equals("deleted")) return;
    requireCurrentScope(store, item);
    store.beginDeletion(id);
    UploadWorker.cancel(context, id);
    DeleteWorker.enqueue(context, id, true);
  }

  static void pending(Context context) {
    CaptureStore store = CaptureStore.get(context);
    for (CaptureStore.Item item : store.items()) {
      if (!item.state.equals("delete_pending")) continue;
      if (!currentScope(store, item)) { failChangedScope(context, store, item); continue; }
      UploadWorker.cancel(context, item.id);
      DeleteWorker.enqueue(context, item.id, false);
    }
  }

  static void scopeChanged(Context context) {
    CaptureStore store = CaptureStore.get(context);
    for (CaptureStore.Item item : store.items())
      if (item.state.equals("delete_pending") && !currentScope(store, item)) failChangedScope(context, store, item);
  }

  private static void failChangedScope(Context context, CaptureStore store, CaptureStore.Item item) {
    String owner = "delete-scope:" + java.util.UUID.randomUUID();
    if (store.claimDeletion(item.id, owner))
      store.deletionUpdate(item.id, owner, "delete_failed", new ScopeChanged().getMessage(), null);
    WorkManager.getInstance(context).cancelUniqueWork("capture-delete-" + item.id);
  }

  static boolean confirmed(CaptureStore.Item item, JSONObject receipt) {
    return receipt != null && item.id.equals(receipt.optString("capture_id"))
        && item.meta.optString("case_id").equals(receipt.optString("case_id"))
        && receipt.optBoolean("deleted") && "deleted".equals(receipt.optString("state"));
  }

  /** One deletion attempt; server confirmation precedes local file cleanup and hidden state. */
  static boolean perform(CaptureStore store, CaptureStore.Item item, CameraApi.Transfer transfer, String owner)
      throws Exception {
    if (!store.deletionOwned(item.id, owner)) return false;
    requireCurrentScope(store, item);
    JSONObject receipt = item.receipt.isEmpty() ? null : new JSONObject(item.receipt);
    if (!confirmed(item, receipt)) {
      if (item.demo()) receipt = new JSONObject().put("capture_id", item.id)
          .put("case_id", item.meta.getString("case_id")).put("deleted", true).put("state", "deleted")
          .put("scope", "local_demo");
      else {
        if (!item.official()) throw new CameraApi.Failure(400, "旧版自绘资料请先在电脑端删除并联系管理员核对，本机资料仍保留");
        receipt = CameraApi.delete(store, item, transfer);
      }
      if (!confirmed(item, receipt)) throw new UploadEngine.ReceiptError("删除回执与这份资料不匹配");
      if (!store.deletionUpdate(item.id, owner, "delete_pending", "", receipt)) return false;
    }
    transfer.check();
    if (!store.deletionOwned(item.id, owner)) return false;
    requireCurrentScope(store, item);
    removeFiles(store, item.id);
    return store.deletionUpdate(item.id, owner, "deleted", "", receipt);
  }

  private static void removeFiles(CaptureStore store, String id) throws IOException {
    if (id == null || id.isEmpty() || id.equals(".") || id.equals("..") || id.contains("/") || id.contains("\\"))
      throw new LocalDeleteError("本机资料路径不匹配，文件已保留，请联系管理员");
    File base = new File(store.context.getFilesDir(), "captures").getCanonicalFile();
    File directory = store.directory(id).getCanonicalFile();
    // Resolve the app's legitimate /data/user -> /data/data parent first, then
    // require the capture itself to be exactly this child, never a sibling link.
    if (!new File(base, id).equals(directory) || !base.equals(directory.getParentFile()))
      throw new LocalDeleteError("本机资料路径不匹配，文件已保留，请联系管理员");
    removeInside(directory, directory);
  }

  private static void removeInside(File file, File root) throws IOException {
    File canonical = file.getCanonicalFile();
    if (!file.getAbsoluteFile().equals(canonical)
        || !canonical.equals(root) && !canonical.getPath().startsWith(root.getPath() + File.separator))
      throw new LocalDeleteError("资料目录包含异常路径，已停止清理，请联系管理员");
    if (!file.exists()) return;
    if (file.isDirectory()) {
      File[] children = file.listFiles();
      if (children == null) throw new LocalDeleteError("服务器删除已确认，本机文件暂时无法清理，请重试删除");
      for (File child : children) removeInside(child, root);
    }
    if (!file.delete() && file.exists()) throw new LocalDeleteError("服务器删除已确认，本机文件暂时无法清理，请重试删除");
  }

  static boolean retryable(Throwable error) {
    if (error instanceof LocalDeleteError || error instanceof ScopeChanged) return false;
    if (error instanceof CameraApi.Failure failure && failure.status == 409) return true;
    return UploadStatus.retryable(error);
  }

  static String explain(Throwable error) {
    if (error instanceof LocalDeleteError || error instanceof ScopeChanged) return error.getMessage();
    if (error instanceof CameraApi.Failure failure) {
      return switch (failure.status) {
        case 401 -> "这份资料所属的设备连接已失效，删除未同步；请联系管理员恢复原设备权限或在工作台处理";
        case 403 -> "这份资料当前无删除权限，删除未同步；请联系管理员核对原设备和项目权限";
        case 404 -> "电脑端尚未支持同步删除或记录接口不存在，请更新工作台后重试";
        case 409 -> "服务器正在处理这份资料，稍后自动继续删除";
        default -> failure.retryable() ? "服务器暂时不可用，删除待同步" : UploadWorker.safeMessage(error);
      };
    }
    if (error instanceof UploadEngine.ReceiptError) return "删除回执不匹配，文件仍保留，请联系管理员核对";
    return retryable(error) ? "网络暂不可用，删除待同步；原片暂时保留" : "删除未完成：" + UploadWorker.safeMessage(error);
  }
}
