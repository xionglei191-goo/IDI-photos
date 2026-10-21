package cn.idi.camera;

import android.app.AlertDialog;
import android.graphics.Color;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import org.json.JSONObject;

final class CaseScreen {
  final MainActivity a;
  final Ui u;
  LinearLayout list;
  String query = "", project = "";
  TextView projectButton;

  CaseScreen(MainActivity a) {
    this.a = a;
    u = a.ui;
  }

  View view() {
    LinearLayout page = u.column();
    page.setPadding(u.dp(20), u.dp(12), u.dp(20), 0);
    LinearLayout header = u.row();
    u.weight(header, u.text("选择案件", 28, Ui.INK, true));
    if (a.store.connected() && !a.demo)
      header.addView(u.icon("refresh", "同步案件", Ui.MUTED, a::sync));
    header.addView(u.icon("settings", "连接与设置", Ui.INK, a::connect));
    page.addView(header);
    u.gap(page, 14);
    if (a.demo) {
      TextView banner = u.text("示例体验 · 照片仅存手机", 14, Ui.ORANGE, true);
      u.pad(banner, 12);
      u.surface(banner, Ui.PALE, Color.TRANSPARENT, 9);
      page.addView(banner);
      u.gap(page, 12);
    }
    projectButton = u.button("全部项目  ▾", false, this::chooseProject);
    projectButton.setGravity(android.view.Gravity.START | android.view.Gravity.CENTER_VERTICAL);
    page.addView(projectButton);
    u.gap(page, 12);
    EditText search = u.input("", "搜索楼栋、房号、案件号", 80);
    search.setSingleLine(true);
    page.addView(search);
    u.gap(page, 12);
    if (!a.status.isEmpty()) {
      page.addView(u.text(a.status, 13, Ui.MUTED, false));
      u.gap(page, 8);
    }
    TextView section = u.text("最近使用", 14, Ui.MUTED, true);
    page.addView(section);
    u.gap(page, 8);
    list = u.column();
    page.addView(u.scroll(list), new LinearLayout.LayoutParams(-1, 0, 1));
    search.addTextChangedListener(
        new TextWatcher() {
          public void beforeTextChanged(CharSequence s, int start, int count, int after) {}

          public void onTextChanged(CharSequence s, int start, int before, int count) {
            query = s.toString().trim().toLowerCase(Locale.ROOT);
            refresh();
          }

          public void afterTextChanged(Editable e) {}
        });
    refresh();
    return page;
  }

  void refresh() {
    list.removeAllViews();
    List<JSONObject> cases = new ArrayList<>(a.cases());
    cases.sort(
        (x, y) ->
            Long.compare(
                a.store.prefs.getLong("recent_" + y.optString("id"), 0),
                a.store.prefs.getLong("recent_" + x.optString("id"), 0)));
    int count = 0;
    for (JSONObject c : cases) {
      String text =
          (c.optString("project_name")
                  + " "
                  + c.optString("location")
                  + " "
                  + c.optString("title")
                  + " "
                  + c.optString("case_number"))
              .toLowerCase(Locale.ROOT);
      if (!project.isEmpty() && !project.equals(c.optString("project_id"))
          || !query.isEmpty() && !text.contains(query)) continue;
      boolean prominent = count == 0 && query.isEmpty();
      LinearLayout card = u.column();
      u.pad(card, 16);
      u.surface(card, prominent ? Ui.PALE : Color.WHITE, prominent ? Ui.ORANGE : Ui.BORDER, 12);
      TextView address = u.text(c.optString("location"), 21, Ui.INK, true);
      card.addView(address);
      u.gap(card, 7);
      LinearLayout info = u.row();
      u.weight(info, u.text(c.optString("project_name"), 13, Ui.MUTED, false));
      TextView stage = u.text(c.optString("stage_label"), 12, Ui.ORANGE, true);
      stage.setPadding(u.dp(6), u.dp(4), u.dp(6), u.dp(4));
      info.addView(stage);
      card.addView(info);
      u.gap(card, 8);
      card.addView(u.text(c.optString("title"), 16, Ui.INK, false));
      u.gap(card, 7);
      String number = c.optString("case_number");
      TextView code =
          u.text(
              number.isEmpty() ? "案件号待补，暂不能拍摄" : number,
              13,
              number.isEmpty() ? Ui.RED : Ui.MUTED,
              false);
      code.setTextIsSelectable(true);
      card.addView(code);
      if (prominent) {
        u.gap(card, 14);
        TextView capture =
            u.button(
                a.selected != null && a.selected.optString("id").equals(c.optString("id"))
                    ? "继续拍照"
                    : "选择并拍照",
                true,
                () -> a.select(c));
        capture.setEnabled(c.optBoolean("capture_enabled"));
        capture.setAlpha(c.optBoolean("capture_enabled") ? 1f : .45f);
        card.addView(capture);
      } else {
        card.setOnClickListener(v -> a.select(c));
        card.setContentDescription("选择案件 " + c.optString("location"));
        card.setFocusable(true);
      }
      list.addView(card, new LinearLayout.LayoutParams(-1, -2));
      u.gap(list, 12);
      count++;
    }
    if (count == 0) {
      u.gap(list, 28);
      list.addView(u.text(cases.isEmpty() ? "先连接 IDI，再开始拍摄" : "没有找到匹配的案件", 20, Ui.INK, true));
      u.gap(list, 12);
      list.addView(
          u.text(
              cases.isEmpty() ? "案件可离线查看；官方拍摄需联网，已拍原片会保留并在联网后补传。" : "试试房号或完整案件号，或切换项目。",
              15,
              Ui.MUTED,
              false));
      u.gap(list, 22);
      if (!a.store.connected()) {
        list.addView(u.button("连接 IDI", true, a::connect));
        u.gap(list, 12);
        list.addView(u.button("先体验示例", false, a::startDemo));
      } else if (cases.isEmpty()) list.addView(u.button("同步案件", true, a::sync));
    } else {
      u.gap(list, 6);
      list.addView(u.text("选好案件，水印自动填写。成片使用左下角小水印。", 13, Ui.MUTED, false));
      u.gap(list, 20);
    }
  }

  void chooseProject() {
    LinkedHashMap<String, String> projects = new LinkedHashMap<>();
    projects.put("", "全部项目");
    for (JSONObject c : a.cases())
      projects.put(c.optString("project_id"), c.optString("project_name"));
    String[] ids = projects.keySet().toArray(new String[0]),
        labels = projects.values().toArray(new String[0]);
    new AlertDialog.Builder(a)
        .setTitle("选择项目")
        .setItems(
            labels,
            (d, index) -> {
              project = ids[index];
              projectButton.setText(labels[index] + "  ▾");
              refresh();
            })
        .show();
  }
}
