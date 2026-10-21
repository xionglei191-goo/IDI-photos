package cn.idi.camera;

import android.app.Dialog;
import android.graphics.Color;
import android.view.Gravity;
import android.view.Window;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import org.json.JSONObject;

final class ConnectionDialog {
  static void show(MainActivity a) {
    Ui u = a.ui;
    Dialog dialog = new Dialog(a);
    LinearLayout page = u.column();
    u.pad(page, 24);
    u.surface(page, Color.WHITE, Color.TRANSPARENT, 18);
    LinearLayout title = u.row();
    u.weight(title, u.text("连接与设置", 23, Ui.INK, true));
    title.addView(u.icon("close", "关闭", Ui.MUTED, dialog::dismiss));
    page.addView(title);
    u.gap(page, 10);
    page.addView(u.text("请管理员在 IDI 中选择你的人员账号和所属项目，再生成手机配对码。", 14, Ui.MUTED, false));
    u.gap(page, 20);
    page.addView(u.text("服务器地址", 14, Ui.INK, true));
    u.gap(page, 8);
    EditText server = u.input(a.store.server(), "例如 https://idi.xionglei.online", 240);
    server.setSingleLine(true);
    server.setInputType(
        android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
    page.addView(server);
    u.gap(page, 16);
    page.addView(u.text("配对码", 14, Ui.INK, true));
    u.gap(page, 8);
    EditText code = u.input("", "输入电脑显示的10位配对码", 10);
    code.setSingleLine(true);
    code.setFilters(
        new android.text.InputFilter[] {
          new android.text.InputFilter.AllCaps(), new android.text.InputFilter.LengthFilter(10)
        });
    page.addView(code);
    u.gap(page, 12);
    TextView error = u.text("", 14, Ui.RED, false);
    page.addView(error);
    u.gap(page, 10);
    TextView connect = u.button("连接并同步", true, () -> {});
    page.addView(connect);
    connect.setOnClickListener(
        v -> {
          connect.setEnabled(false);
          connect.setAlpha(.5f);
          error.setText("正在连接…");
          String address = server.getText().toString(), pairCode = code.getText().toString().trim();
          a.io.execute(
              () -> {
                try {
                  String normalized = CameraApi.normalizeServer(address);
                  JSONObject pair =
                      CameraApi.request(
                          normalized,
                          "/api/camera/pair",
                          "",
                          new JSONObject().put("code", pairCode));
                  a.store.connect(normalized, pair);
                  a.runOnUiThread(
                      () -> {
                        a.demo = false;
                        a.selected = null;
                        dialog.dismiss();
                        a.show("cases");
                        a.sync();
                      });
                } catch (Exception ex) {
                  a.runOnUiThread(
                      () -> {
                        error.setText(UploadWorker.safeMessage(ex));
                        connect.setEnabled(true);
                        connect.setAlpha(1);
                      });
                }
              });
        });
    if (a.store.connected()) {
      u.gap(page, 18);
      page.addView(
          u.text("已配对：" + a.store.prefs.getString("device_name", "本机")
              + " · " + a.store.prefs.getString("user_name", "请重新绑定人员"), 14, Ui.MUTED, false));
      u.gap(page, 10);
      page.addView(
          u.button(
              "使用已连接的案件",
              false,
              () -> {
                a.demo = false;
                a.store.prefs.edit().putBoolean("demo", false).apply();
                a.restoreSelection();
                dialog.dismiss();
                a.show("cases");
                a.sync();
              }));
    }
    u.gap(page, 12);
    page.addView(
        u.button(
            "示例体验（不上传）",
            false,
            () -> {
              dialog.dismiss();
              a.startDemo();
            }));
    u.gap(page, 16);
    page.addView(
        u.text(
            "IDI 水印相机  " + BuildConfig.VERSION_NAME + "\n照片保存在本机；卸载或清除应用数据前请先上传或导出。",
            12,
            Ui.MUTED,
            false));
    dialog.setContentView(u.scroll(page));
    Window w = dialog.getWindow();
    if (w != null) {
      w.setBackgroundDrawableResource(android.R.color.transparent);
      w.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
      w.setLayout(-1, -2);
      w.setGravity(Gravity.BOTTOM);
    }
    dialog.show();
    if (w != null) w.setLayout(-1, (int) (a.getResources().getDisplayMetrics().heightPixels * .87));
  }
}
