package cn.idi.camera;

import android.app.Dialog;
import android.app.Activity;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

final class WatermarkEditor {
  static final class Draft {
    String group, part, content, role;
    boolean repeat;

    Draft(String group, String part, String content, String role, boolean repeat) {
      this.group = group; this.part = part; this.content = content; this.role = role; this.repeat = repeat;
    }

    static Draft from(JSONObject meta) {
      return new Draft(meta.optString("photo_group"), meta.optString("part"),
          meta.optString("content"), meta.optString("photo_group").endsWith("checkin")
              ? meta.optString("role") : "", true);
    }

    JSONObject saved() throws Exception {
      return new JSONObject().put("group", group).put("part", part).put("content", content).put("role", role);
    }
  }

  interface Save { void accept(Draft draft) throws Exception; }

  static void show(MainActivity a) {
    show(a, () -> {});
  }

  static void show(MainActivity a, Runnable afterSave) {
    if (a.selected == null) return;
    show(a, a.selected, new Draft(a.group, a.part, a.content, a.checkinRole, a.repeat),
        draft -> {
          a.group = draft.group; a.part = draft.part; a.content = draft.content;
          a.checkinRole = draft.role; a.repeat = draft.repeat;
          a.saveDraft();
          if (a.cameraScreen != null) {
            a.cameraScreen.refreshModes(); a.cameraScreen.tick();
          }
          afterSave.run();
        }, () -> {});
  }

  static Dialog show(Activity a, JSONObject caseInfo, Draft draft, Save save, Runnable dismissed) {
    Ui u = new Ui(a);
    boolean video = "video".equals(caseInfo.optString("media_type"));
    Dialog dialog = new Dialog(a);
    LinearLayout page = u.column();
    u.pad(page, 20);
    u.surface(page, Color.WHITE, Color.TRANSPARENT, 18);
    LinearLayout heading = u.row();
    u.weight(heading, u.text("快速改水印", 22, Ui.INK, true));
    heading.addView(u.icon("close", "关闭水印编辑", Ui.MUTED, dialog::dismiss));
    page.addView(heading);
    u.gap(page, 10);
    LinearLayout summary = u.column();
    u.pad(summary, 12);
    u.surface(summary, Ui.LIGHT, Color.TRANSPARENT, 9);
    summary.addView(
        u.text(
            caseInfo.optString("project_name") + " · " + caseInfo.optString("location"),
            16,
            Ui.INK,
            true));
    u.gap(summary, 6);
    TextView number = u.text(caseInfo.optString("case_number"), 13, Ui.MUTED, false);
    number.setTextIsSelectable(true);
    summary.addView(number);
    u.gap(summary, 6);
    summary.addView(u.text("时间和地点由官方相机生成", 12, Ui.MUTED, false));
    page.addView(summary);
    u.gap(page, 20);
    String[] selected = {draft.group}, role = {draft.role};
    List<TextView> choices = new ArrayList<>();
    LinearLayout roles = u.column();
    page.addView(u.text("拍摄环节", 15, Ui.INK, true));
    u.gap(page, 8);
    for (int start = 0; start < 5; start += 3) {
      LinearLayout row = u.row();
      for (int i = start; i < Math.min(start + 3, 5); i++) {
        int index = i;
        TextView b =
            u.chip(CameraData.LABELS[i], selected[0].equals(CameraData.GROUPS[i]), () -> {});
        choices.add(b);
        b.setOnClickListener(
            v -> {
              selected[0] = CameraData.GROUPS[index];
              for (int j = 0; j < choices.size(); j++) {
                boolean active = selected[0].equals(CameraData.GROUPS[j]);
                choices.get(j).setTextColor(active ? Ui.ORANGE : Ui.INK);
                u.ripple(
                    choices.get(j), active ? Ui.PALE : Ui.LIGHT, active ? Ui.ORANGE : Ui.BORDER, 9);
              }
              roles.setVisibility(selected[0].endsWith("checkin") ? View.VISIBLE : View.GONE);
            });
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, -2, 1);
        if (i > start) p.leftMargin = u.dp(8);
        row.addView(b, p);
      }
      page.addView(row);
      u.gap(page, 8);
    }
    roles.setVisibility(selected[0].endsWith("checkin") ? View.VISIBLE : View.GONE);
    u.gap(roles, 8);
    roles.addView(u.text("打卡用途", 15, Ui.INK, true));
    u.gap(roles, 8);
    LinearLayout roleButtons = u.row();
    TextView building = u.chip("楼栋打卡", "building_checkin".equals(role[0]), () -> {}),
        door = u.chip("入户门打卡", "door_checkin".equals(role[0]), () -> {});
    Runnable updateRoles =
        () -> {
          for (TextView b : new TextView[] {building, door}) {
            boolean active =
                b == building ? "building_checkin".equals(role[0]) : "door_checkin".equals(role[0]);
            b.setTextColor(active ? Ui.ORANGE : Ui.INK);
            u.ripple(b, active ? Ui.PALE : Ui.LIGHT, active ? Ui.ORANGE : Ui.BORDER, 9);
          }
        };
    building.setOnClickListener(
        v -> {
          role[0] = "building_checkin";
          updateRoles.run();
        });
    door.setOnClickListener(
        v -> {
          role[0] = "door_checkin";
          updateRoles.run();
        });
    roleButtons.addView(building, new LinearLayout.LayoutParams(0, -2, 1));
    LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(0, -2, 1);
    rp.leftMargin = u.dp(8);
    roleButtons.addView(door, rp);
    roles.addView(roleButtons);
    page.addView(roles);
    u.gap(page, 16);
    page.addView(u.text("维修部位", 15, Ui.INK, true));
    u.gap(page, 8);
    EditText part = u.input(draft.part, "选择或输入实际部位", 80);
    part.setSingleLine(true);
    LinkedHashSet<String> parts = new LinkedHashSet<>();
    JSONArray configured = caseInfo.optJSONArray("parts");
    if (configured != null)
      for (int i = 0; i < Math.min(3, configured.length()); i++) parts.add(configured.optString(i));
    parts.add("卫生间");
    parts.add("厨房");
    parts.add("客厅");
    parts.add("其他");
    LinearLayout partRow = u.row();
    int pcount = 0;
    for (String value : parts) {
      if (pcount++ >= 4) break;
      TextView b =
          u.chip(
              value,
              value.equals(draft.part),
              () -> {
                if (value.equals("其他")) {
                  part.setText("");
                  part.requestFocus();
                } else part.setText(value);
              });
      LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1);
      if (partRow.getChildCount() > 0) params.leftMargin = u.dp(6);
      partRow.addView(b, params);
    }
    page.addView(partRow);
    u.gap(page, 8);
    page.addView(part);
    u.gap(page, 16);
    page.addView(u.text(video ? "视频内容" : "照片内容", 15, Ui.INK, true));
    u.gap(page, 8);
    EditText content = u.input(draft.content, video ? "填写本段视频的实际内容" : "填写本张照片的实际内容", 240);
    content.setMinLines(2);
    content.setMaxLines(4);
    content.setInputType(
        android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);
    page.addView(content);
    LinearLayout footer = u.column();
    footer.setPadding(u.dp(20), u.dp(4), u.dp(20), u.dp(16));
    CheckBox reuse = new CheckBox(a);
    reuse.setText("本案后续拍摄沿用");
    reuse.setTextSize(15);
    reuse.setTextColor(Ui.INK);
    reuse.setChecked(draft.repeat);
    reuse.setMinHeight(u.dp(48));
    footer.addView(reuse);
    TextView error = u.text("", 14, Ui.RED, false);
    footer.addView(error);
    u.gap(footer, 8);
    footer.addView(
        u.button(
            video ? "保存并继续录像" : "保存并继续拍照",
            true,
            () -> {
              String text = content.getText().toString().trim();
              if (text.isEmpty()) {
                error.setText(video ? "请填写这段视频的实际内容" : "请填写这张照片的实际内容");
                return;
              }
              if (selected[0].endsWith("checkin") && role[0].isEmpty()) {
                error.setText("请选择楼栋或入户门打卡用途");
                return;
              }
              try {
                save.accept(new Draft(selected[0], part.getText().toString().trim(), text,
                    role[0], reuse.isChecked()));
                dialog.dismiss();
              } catch (Exception failure) {
                error.setText(UploadWorker.safeMessage(failure));
              }
            }));
    u.gap(page, 12);
    LinearLayout sheet = u.column();
    u.surface(sheet, Color.WHITE, Color.TRANSPARENT, 18);
    sheet.addView(u.scroll(page), new LinearLayout.LayoutParams(-1, 0, 1));
    u.divider(sheet);
    sheet.addView(footer);
    dialog.setContentView(sheet);
    dialog.setOnDismissListener(d -> dismissed.run());
    Window window = dialog.getWindow();
    if (window != null) {
      window.setBackgroundDrawableResource(android.R.color.transparent);
      window.setGravity(Gravity.BOTTOM);
      window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
    }
    dialog.show();
    if (window != null)
      window.setLayout(-1, (int) (a.getResources().getDisplayMetrics().heightPixels * .84));
    return dialog;
  }
}
