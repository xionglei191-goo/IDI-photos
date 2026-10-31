package cn.idi.camera;

import android.app.AlertDialog;
import android.graphics.Color;
import android.text.Editable;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import org.json.JSONObject;

final class CaseScreen {
  static final String[] STAGES = {"", "survey", "repair", "acceptance"};
  static final String[] STAGE_LABELS = {"全部状态", "查勘", "施工", "验收"};
  final MainActivity a;
  final Ui u;
  final String filterKey;
  LinearLayout list;
  String query, project, projectName, stage;
  TextView projectButton, stageButton;

  CaseScreen(MainActivity a) {
    this.a = a;
    u = a.ui;
    // Screens are rebuilt on navigation and sync. Keep filters with the paired
    // device, separately from demo mode and any later connection.
    filterKey = "case_filters_" + (a.demo ? "demo" : a.store.server() + "|" + a.store.device()) + "_";
    query = a.store.prefs.getString(filterKey + "query", "");
    project = a.store.prefs.getString(filterKey + "project", "");
    projectName = a.store.prefs.getString(filterKey + "project_name", "");
    stage = a.store.prefs.getString(filterKey + "stage", "");
    if (!Arrays.asList(STAGES).contains(stage)) stage = "";
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
    LinearLayout filters = u.row();
    projectButton = u.button("全部项目  ▾", false, this::chooseProject);
    projectButton.setGravity(android.view.Gravity.START | android.view.Gravity.CENTER_VERTICAL);
    projectButton.setSingleLine(true);
    projectButton.setMinHeight(u.dp(50));
    projectButton.setEllipsize(TextUtils.TruncateAt.END);
    u.weight(filters, projectButton);
    stageButton = u.button("全部状态  ▾", false, this::chooseStage);
    stageButton.setSingleLine(true);
    stageButton.setMinHeight(u.dp(50));
    LinearLayout.LayoutParams stageParams = new LinearLayout.LayoutParams(-2, -2);
    stageParams.leftMargin = u.dp(10);
    filters.addView(stageButton, stageParams);
    page.addView(filters);
    u.gap(page, 12);
    EditText search = u.input(query, "搜索楼栋、房号、案件号", 80);
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
            query = s.toString();
            saveFilters();
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
    updateFilterButtons(cases);
    String search = query.trim().toLowerCase(Locale.ROOT);
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
      if ((!project.isEmpty() && !project.equals(c.optString("project_id")))
          || (!stage.isEmpty() && !stage.equals(c.optString("workflow_stage")))
          || (!search.isEmpty() && !text.contains(search))) continue;
      boolean prominent = count == 0 && search.isEmpty();
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
              cases.isEmpty() ? "案件可离线查看；官方拍摄需联网，已拍原片会保留并在联网后补传。" : "调整搜索条件，或切换项目、状态。",
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
    for (JSONObject c : a.cases()) {
      String id = c.optString("project_id");
      if (!id.isEmpty()) projects.put(id, c.optString("project_name"));
    }
    String[] ids = projects.keySet().toArray(new String[0]),
        labels = projects.values().toArray(new String[0]);
    new AlertDialog.Builder(a)
        .setTitle("选择项目")
        .setSingleChoiceItems(
            labels,
            Arrays.asList(ids).indexOf(project),
            (d, index) -> {
              project = ids[index];
              projectName = project.isEmpty() ? "" : labels[index];
              saveFilters();
              refresh();
              d.dismiss();
            })
        .setNegativeButton("取消", null)
        .show();
  }

  void chooseStage() {
    new AlertDialog.Builder(a)
        .setTitle("选择状态")
        .setSingleChoiceItems(
            STAGE_LABELS,
            Arrays.asList(STAGES).indexOf(stage),
            (d, index) -> {
              stage = STAGES[index];
              saveFilters();
              refresh();
              d.dismiss();
            })
        .setNegativeButton("取消", null)
        .show();
  }

  void saveFilters() {
    a.store.prefs.edit()
        .putString(filterKey + "query", query)
        .putString(filterKey + "project", project)
        .putString(filterKey + "project_name", projectName)
        .putString(filterKey + "stage", stage)
        .apply();
  }

  private void updateFilterButtons(List<JSONObject> cases) {
    for (JSONObject c : cases) {
      if (!project.isEmpty() && project.equals(c.optString("project_id"))) {
        String name = c.optString("project_name");
        if (!name.isEmpty()) projectName = name;
        break;
      }
    }
    String projectLabel = project.isEmpty() ? "全部项目" : projectName.isEmpty() ? "已选项目" : projectName;
    String stageLabel = STAGE_LABELS[Arrays.asList(STAGES).indexOf(stage)];
    projectButton.setText(a.getString(R.string.case_filter_value, projectLabel));
    projectButton.setContentDescription("项目筛选：" + projectLabel);
    stageButton.setText(a.getString(R.string.case_filter_value, stageLabel));
    stageButton.setContentDescription("状态筛选：" + stageLabel);
    projectButton.setSelected(!project.isEmpty());
    stageButton.setSelected(!stage.isEmpty());
    projectButton.setTextColor(project.isEmpty() ? Ui.INK : Ui.ORANGE);
    stageButton.setTextColor(stage.isEmpty() ? Ui.INK : Ui.ORANGE);
    u.ripple(projectButton, project.isEmpty() ? Color.WHITE : Ui.PALE,
        project.isEmpty() ? Ui.BORDER : Ui.ORANGE, 10);
    u.ripple(stageButton, stage.isEmpty() ? Color.WHITE : Ui.PALE,
        stage.isEmpty() ? Ui.BORDER : Ui.ORANGE, 10);
  }
}
