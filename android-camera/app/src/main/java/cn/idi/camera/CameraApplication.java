package cn.idi.camera;

import android.app.Application;
import android.net.ConnectivityManager;
import android.net.Network;
import androidx.annotation.NonNull;
import androidx.work.Configuration;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class CameraApplication extends Application implements Configuration.Provider {
  // Only these two threads run upload workers; WorkManager's bookkeeping has its own executor.
  private final ExecutorService uploads = Executors.newFixedThreadPool(2);

  @NonNull @Override public Configuration getWorkManagerConfiguration() {
    return new Configuration.Builder().setExecutor(uploads).build();
  }

  @Override public void onCreate() {
    super.onCreate();
    ConnectivityManager cm = getSystemService(ConnectivityManager.class);
    if (cm == null) return;
    cm.registerDefaultNetworkCallback(new ConnectivityManager.NetworkCallback() {
      @Override public void onAvailable(@NonNull Network network) {
        CaptureStore.io.execute(() -> UploadWorker.networkRecovered(CameraApplication.this));
      }
    });
  }
}
