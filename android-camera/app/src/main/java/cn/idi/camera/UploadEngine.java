package cn.idi.camera;

import java.io.IOException;
import org.json.JSONObject;

/** One attempt, shared by the persistent worker and transport fault tests. */
final class UploadEngine {
  static final class ReceiptError extends IOException {
    ReceiptError(String message) { super(message); }
  }

  static JSONObject transfer(CaptureStore store, CaptureStore.Item item,
      CameraApi.Transfer transfer, UploadStatus status) throws Exception {
    transfer.bindUpload(store, item.id);
    CameraApi.validateFile(store.original(item.id), item.video());
    if (store.token(item.device).isEmpty()) throw new CameraApi.Failure(401, "设备连接已失效");
    transfer.observer = new CameraApi.Observer() {
      public void phase(String stage) throws Exception { status.stage(stage); }
      public void completed(String operation, JSONObject timing) throws Exception { status.timing(operation, timing); }
    };
    status.stage("checking");
    JSONObject receipt;
    try {
      receipt = query(store, item, transfer, "receipt_check");
    } catch (CameraApi.Failure error) {
      // An unavailable receipt service is not evidence that the upload is absent.
      if (error.status != 404 || !item.receipt.isEmpty()) throw error;
      receipt = null;
    }
    if (receipt == null) {
      try {
        receipt = CameraApi.upload(store, item, transfer, (sent, total) -> {
          status.progress(sent, total);
        });
      } catch (Exception error) {
        if (transfer.cancelled() || !UploadStatus.retryable(error)) throw error;
        status.noteError(error);
        status.stage("reconciling");
        try {
          receipt = query(store, item, transfer, "receipt_reconcile");
        } catch (Exception lookup) {
          // Never resurrect deleted records; preserve actionable authentication errors.
          if (lookup instanceof CameraApi.Failure && ((CameraApi.Failure) lookup).status != 404
              && !UploadStatus.retryable(lookup)) throw lookup;
          throw error;
        }
      }
    }
    String media = receipt.optString("media_type", "image");
    if (!item.id.equals(receipt.optString("capture_id"))
        || !(item.video() ? "video" : "image").equals(media)
        || !OfficialPhotoWriter.sha256(store.original(item.id)).equals(receipt.optString("original_hash")))
      throw new ReceiptError("官方原片回执编号、类型或哈希不匹配");
    return receipt;
  }

  private static JSONObject query(CaptureStore store, CaptureStore.Item item, CameraApi.Transfer transfer, String operation) throws Exception {
    return CameraApi.request(item.server, "/api/camera/official-captures/" + item.id,
        store.token(item.device), null, transfer, operation);
  }
}
