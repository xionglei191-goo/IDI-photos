package cn.idi.camera;

import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import java.util.List;
import java.util.concurrent.ExecutorService;
import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends ComponentActivity {
  Ui ui;
  CaptureStore store;
  final ExecutorService io = CaptureStore.io;
  final Handler handler = new Handler(Looper.getMainLooper());
  String screen = "cases",
      group = "construction",
      part = "卫生间",
      content = "",
      checkinRole = "",
      status = "";
  boolean demo, repeat = true, busy, syncing;
  JSONObject selected;
  LinearLayout root;
  CameraScreen cameraScreen;
  OfficialCameraScreen officialScreen;
  RecordsScreen recordsScreen;
  ActivityResultLauncher<String> permission;
  ActivityResultLauncher<Intent> officialCapture;
  ActivityResultLauncher<String[]> sdkPermissions;
  final Runnable ticker =
      new Runnable() {
        public void run() {
          if (cameraScreen != null) cameraScreen.tick();
          if (recordsScreen != null) recordsScreen.tick();
          handler.postDelayed(this, 1000);
        }
      };

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    // Some launchers create another entry Activity above an in-progress SDK camera.
    // Keep the original task and its ActivityResult owner instead of opening a second flow.
    if (!isTaskRoot()
        && Intent.ACTION_MAIN.equals(getIntent().getAction())
        && getIntent().hasCategory(Intent.CATEGORY_LAUNCHER)) {
      finish();
      return;
    }
    ui = new Ui(this);
    store = CaptureStore.get(this);
    demo = store.prefs.getBoolean("demo", false);
    WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
    permission =
        registerForActivityResult(
            new ActivityResultContracts.RequestPermission(),
            granted -> {
              if (granted && cameraScreen != null) cameraScreen.bind();
              else if (granted && officialScreen != null) officialScreen.start();
              else if (!granted) toast("相机权限未开启，可在手机设置中允许后再拍摄");
            });
    sdkPermissions =
        registerForActivityResult(
            new ActivityResultContracts.RequestMultiplePermissions(),
            permissions -> {
              if (Boolean.TRUE.equals(permissions.get(android.Manifest.permission.CAMERA))
                  && Boolean.TRUE.equals(
                      permissions.get(android.Manifest.permission.ACCESS_FINE_LOCATION))) {
                if (officialScreen != null) officialScreen.start();
              } else toast("今日官方拍摄需要相机和精确位置权限，请允许后再拍摄");
            });
    officialCapture =
        registerForActivityResult(
            new ActivityResultContracts.StartActivityForResult(),
            result -> {
              String pending = store.prefs.getString("pending_official_capture", "");
              String returned = result.getData() == null
                  ? null : result.getData().getStringExtra("capture_id");
              String id = returned == null || returned.isEmpty() ? pending : returned;
              if (id.equals(pending))
                store.prefs.edit().remove("pending_official_capture").commit();
              busy = false;
              if (id.isEmpty()) {
                toast("未找到本次官方拍摄信息，请重新拍摄");
                return;
              }
              CaptureStore.Item item = store.item(id);
              if (item != null && item.official() && store.original(id).isFile()) {
                recoverInterrupted();
                UploadWorker.pending(this);
                show("records");
                toast("官方原片已保存，等待上传到电脑");
              } else if (item != null && item.state.equals("awaiting_sdk")) {
                store.cancelEmptyOfficialCapture(id);
              }
              if (item == null || !store.original(id).isFile()) show("cases");
              loadDraft();
            });
    getOnBackPressedDispatcher()
        .addCallback(
            this,
            new OnBackPressedCallback(true) {
              @Override
              public void handleOnBackPressed() {
                if (!screen.equals("cases")) show("cases");
                else finish();
              }
            });
    restoreSelection();
    busy = state != null && state.getBoolean("busy", false);
    show(state == null ? "cases" : state.getString("screen", "cases"));
    if (state == null && isTaskRoot()) io.execute(store::recoverAbandonedOfficialCaptures);
    recoverInterrupted();
  }

  void recoverInterrupted() {
    io.execute(
        () -> {
          for (CaptureStore.Item item : store.items()) {
            if ((item.state.equals("capturing") || item.state.equals("rendering")
                || item.official() && (store.original(item.id).isFile()
                    || item.video() && OfficialVideoWriter.hasCandidate(store, item.id))
                    && (item.state.equals("awaiting_sdk") || item.state.equals("capture_failed")
                        || item.state.equals("cancelled")))
                && store.activeCaptures.add(item.id)) {
              try {
                if (item.video()) OfficialVideoWriter.recover(store, item.id);
                else if (item.official()) OfficialPhotoWriter.recover(store, item.id);
                else
                  WatermarkRenderer.render(
                      store.original(item.id),
                      store.stamped(item.id),
                      store.thumbnail(item.id),
                      item.meta);
                store.state(item.id, item.demo() ? "local" : "queued", "");
              } catch (Exception e) {
                store.state(item.id, "capture_failed", "上次拍摄中断：" + UploadWorker.safeMessage(e));
              } finally {
                store.activeCaptures.remove(item.id);
              }
            }
          }
          UploadWorker.pending(this);
        });
  }

  @Override
  protected void onSaveInstanceState(Bundle state) {
    state.putString("screen", screen);
    state.putBoolean("busy", busy);
    super.onSaveInstanceState(state);
  }

  @Override
  protected void onResume() {
    super.onResume();
    if (officialScreen != null) officialScreen.maybeStart();
    handler.removeCallbacks(ticker);
    handler.post(ticker);
  }

  @Override
  protected void onPause() {
    handler.removeCallbacks(ticker);
    super.onPause();
  }

  @Override
  protected void onDestroy() {
    handler.removeCallbacksAndMessages(null);
    if (cameraScreen != null) cameraScreen.close();
    super.onDestroy();
  }

  void toast(String text) {
    Toast.makeText(this, text, Toast.LENGTH_LONG).show();
  }

  List<JSONObject> cases() {
    return demo ? demoCases() : store.cases();
  }

  void restoreSelection() {
    String id = store.prefs.getString(demo ? "demo_selected_case" : "selected_case", "");
    selected = null;
    for (JSONObject c : cases())
      if (c.optString("id").equals(id)) {
        selected = c;
        loadDraft();
        break;
      }
  }

  void select(JSONObject value) {
    if (busy) {
      toast("正在保存照片，请稍候");
      return;
    }
    if (!value.optBoolean("capture_enabled")) {
      toast("本案尚无案件号，请先在工作台补齐");
      return;
    }
    selected = value;
    store
        .prefs
        .edit()
        .putString(demo ? "demo_selected_case" : "selected_case", value.optString("id"))
        .putLong("recent_" + value.optString("id"), System.currentTimeMillis())
        .apply();
    loadDraft();
    show("camera");
  }

  String draftKey() {
    return "draft_" + (selected == null ? "" : selected.optString("id"));
  }

  void loadDraft() {
    if (selected == null) return;
    group =
        "repair".equals(selected.optString("workflow_stage")) ? "construction" : "survey_problem";
    part =
        selected.optJSONArray("parts") == null
            ? ""
            : selected.optJSONArray("parts").optString(0, "");
    content = selected.optString("title");
    if (content.length() > 240) content = "";
    checkinRole = "";
    repeat = true;
    try {
      JSONObject draft = new JSONObject(store.prefs.getString(draftKey(), "{}"));
      group = draft.optString("group", group);
      part = draft.optString("part", part);
      content = draft.optString("content", content);
      checkinRole = draft.optString("role", "");
    } catch (Exception ignored) {
    }
  }

  void saveDraft() {
    if (!repeat) return;
    try {
      store
          .prefs
          .edit()
          .putString(
              draftKey(),
              new JSONObject()
                  .put("group", group)
                  .put("part", part)
                  .put("content", content)
                  .put("role", checkinRole)
                  .toString())
          .apply();
    } catch (Exception e) {
      toast("填写内容未保存，请重新确认");
    }
  }

  JSONObject metadata() throws Exception {
    return CameraData.metadata(selected, group, part, content, checkinRole);
  }

  void show(String next) {
    if (next.equals("camera") && selected == null) next = "cases";
    if (cameraScreen != null) {
      cameraScreen.close();
      cameraScreen = null;
    }
    recordsScreen = null;
    officialScreen = null;
    screen = next;
    boolean dark = screen.equals("camera");
    root = ui.column();
    root.setBackgroundColor(dark ? Ui.DARK : Color.WHITE);
    setContentView(root);
    WindowCompat.getInsetsController(getWindow(), root).setAppearanceLightStatusBars(!dark);
    WindowCompat.getInsetsController(getWindow(), root).setAppearanceLightNavigationBars(!dark);
    ViewCompat.setOnApplyWindowInsetsListener(
        root,
        (view, insets) -> {
          Insets bars =
              insets.getInsets(
                  WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
          view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
          return insets;
        });
    if (dark) {
      if (demo) {
        cameraScreen = new CameraScreen(this);
        root.addView(cameraScreen.view(), new LinearLayout.LayoutParams(-1, 0, 1));
        CameraScreen opened = cameraScreen;
        root.post(() -> {
          if (!isFinishing() && !isDestroyed() && cameraScreen == opened) WatermarkEditor.show(this);
        });
      } else {
        officialScreen = new OfficialCameraScreen(this);
        root.addView(officialScreen.view(), new LinearLayout.LayoutParams(-1, 0, 1));
      }
    } else {
      View page;
      if (screen.equals("records")) {
        recordsScreen = new RecordsScreen(this);
        page = recordsScreen.view();
      } else page = new CaseScreen(this).view();
      root.addView(page, new LinearLayout.LayoutParams(-1, 0, 1));
      navigation();
    }
  }

  private void navigation() {
    ui.divider(root);
    LinearLayout row = ui.row();
    row.setPadding(ui.dp(12), ui.dp(4), ui.dp(12), ui.dp(2));
    String[] screens = {"cases", "camera", "records"}, labels = {"案件", "拍照", "记录"};
    for (int i = 0; i < screens.length; i++) {
      String target = screens[i];
      int color = screen.equals(target) ? Ui.ORANGE : Ui.MUTED;
      LinearLayout item = ui.column();
      item.setGravity(Gravity.CENTER);
      ui.ripple(item, Color.WHITE, Color.TRANSPARENT, 8);
      Ui.Glyph icon = new Ui.Glyph(this, target, color);
      item.addView(icon, new LinearLayout.LayoutParams(ui.dp(32), ui.dp(32)));
      TextView label = ui.text(labels[i], 12, color, true);
      label.setGravity(Gravity.CENTER);
      item.addView(label);
      item.setMinimumHeight(ui.dp(58));
      item.setContentDescription(labels[i]);
      item.setFocusable(true);
      item.setOnClickListener(
          v -> {
            if (target.equals("camera") && selected == null) toast("请先选择一个案件");
            show(target);
          });
      row.addView(item, new LinearLayout.LayoutParams(0, -2, 1));
    }
    root.addView(row);
  }

  void connect() {
    ConnectionDialog.show(this);
  }

  void sync() {
    if (syncing) return;
    if (!store.connected()) {
      connect();
      return;
    }
    syncing = true;
    status = "正在同步案件…";
    if (screen.equals("cases")) show("cases");
    io.execute(
        () -> {
          String message;
          try {
            JSONObject response =
                CameraApi.request(
                    store.server(), "/api/camera/cases", store.token(store.device()), null);
            store.replaceCases(response.getJSONArray("items"));
            message = "已同步 " + response.optInt("total") + " 个案件";
          } catch (Exception error) {
            message = "同步失败，保留上次案件：" + UploadWorker.safeMessage(error);
          }
          String result = message;
          runOnUiThread(
              () -> {
                syncing = false;
                status = result;
                restoreSelection();
                if (screen.equals("cases")) show("cases");
                else toast(result);
              });
        });
  }

  void startDemo() {
    demo = true;
    store.prefs.edit().putBoolean("demo", true).apply();
    selected = null;
    show("cases");
  }

  void retry(CaptureStore.Item item) {
    if (item.demo()) {
      toast("示例照片只保存在手机，可预览或导出");
      return;
    }
    if (item.state.equals("capture_failed") || item.state.equals("camera_error")
        || item.state.equals("cancelled")) {
      toast("拍摄未完成，请重新拍摄；已有文件仍保留在手机");
      return;
    }
    if (!item.official()) {
      toast("旧版自绘水印没有官方验真，请使用今日 SDK 重新拍摄");
      return;
    }
    if (item.state.equals("rejected")) {
      toast("官方验真未通过，请重新拍摄");
      return;
    }
    if (item.state.equals("verification_error")) {
      io.execute(
          () -> {
            try {
              JSONObject receipt =
                  CameraApi.request(
                      item.server,
                      "/api/camera/official-captures/" + item.id + "/retry",
                      store.token(item.device),
                      new JSONObject());
              store.receipt(item.id, "verifying", receipt);
              UploadWorker.enqueue(this, item.id, true);
            } catch (Exception error) {
              runOnUiThread(() -> toast("未重试：" + UploadWorker.safeMessage(error)));
            }
          });
      return;
    }
    store.state(item.id, "queued", "");
    UploadWorker.enqueue(this, item.id, true);
    if (recordsScreen != null) recordsScreen.refresh();
  }

  static List<JSONObject> demoCases() {
    try {
      return CameraData.objects(
          new JSONArray(
              "[{\"id\":\"demo-201\",\"project_id\":\"demo\",\"project_name\":\"铂悦府（示例）\",\"case_number\":\"ZLBH202610190000000001\",\"location\":\"1栋"
                  + " · 201室\",\"title\":\"卫生间瓷砖修补\",\"parts\":[\"卫生间\"],\"workflow_stage\":\"repair\",\"stage_label\":\"施工中\",\"capture_enabled\":true},{\"id\":\"demo-302\",\"project_id\":\"demo\",\"project_name\":\"铂悦府（示例）\",\"case_number\":\"ZLBH202610190000000002\",\"location\":\"2栋"
                  + " · 302室\",\"title\":\"厨房渗水\",\"parts\":[\"厨房\"],\"workflow_stage\":\"survey\",\"stage_label\":\"待查勘\",\"capture_enabled\":true},{\"id\":\"demo-508\",\"project_id\":\"demo\",\"project_name\":\"铂悦府（示例）\",\"case_number\":\"ZLBH202610190000000003\",\"location\":\"3栋"
                  + " · 508室\",\"title\":\"客厅墙面修复\",\"parts\":[\"客厅\"],\"workflow_stage\":\"acceptance\",\"stage_label\":\"待验收\",\"capture_enabled\":true}]"));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
