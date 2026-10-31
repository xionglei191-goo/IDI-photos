package cn.idi.camera;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import androidx.core.app.NotificationCompat;
import androidx.work.ForegroundInfo;
import org.json.JSONObject;

final class UploadNotification {
  private static final String CHANNEL = "idi-photo-upload";
  final Context context;
  final int id;
  final NotificationManager manager;

  UploadNotification(Context context, String captureId) {
    this.context = context;
    id = 10000 + (captureId.hashCode() & 0x3fffffff);
    manager = context.getSystemService(NotificationManager.class);
    manager.createNotificationChannel(new NotificationChannel(CHANNEL, "原片上传", NotificationManager.IMPORTANCE_LOW));
  }

  private Notification notification(JSONObject info) {
    Intent intent = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
    PendingIntent open = PendingIntent.getActivity(context, 0, intent,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    boolean sending = "sending".equals(info.optString("stage"));
    return new NotificationCompat.Builder(context, CHANNEL).setSmallIcon(R.drawable.ic_upload)
        .setContentTitle("IDI 原片上传").setContentText(UploadStatus.uploading(info))
        .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true)
        .setCategory(NotificationCompat.CATEGORY_PROGRESS)
        .setProgress(100, UploadStatus.percent(info), !sending).build();
  }

  ForegroundInfo foreground(JSONObject info) {
    return Build.VERSION.SDK_INT >= 29
        ? new ForegroundInfo(id, notification(info), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        : new ForegroundInfo(id, notification(info));
  }

  void update(JSONObject info) {
    try { manager.notify(id, notification(info)); } catch (SecurityException ignored) { }
  }
}
