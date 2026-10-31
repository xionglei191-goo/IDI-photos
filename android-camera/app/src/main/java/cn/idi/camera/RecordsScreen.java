package cn.idi.camera;

import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.ProgressBar;
import java.util.HashMap;
import java.util.Map;
import java.util.List;

final class RecordsScreen {
  final MainActivity a;
  final Ui u;
  LinearLayout list;
  TextView summary;
  String filter = "all", signature = "";
  int limit = 60;
  final Map<String, TextView> statusLabels = new HashMap<>(), transferLabels = new HashMap<>();
  final Map<String, ProgressBar> progressBars = new HashMap<>();

  RecordsScreen(MainActivity a) {
    this.a = a;
    u = a.ui;
  }

  View view() {
    LinearLayout page = u.column();
    page.setPadding(u.dp(20), u.dp(12), u.dp(20), u.dp(8));
    LinearLayout header = u.row();
    u.weight(header, u.text("上传记录", 28, Ui.INK, true));
    header.addView(
        u.icon(
            "refresh",
            "刷新上传记录",
            Ui.MUTED,
            () -> {
              UploadWorker.pending(a);
              refresh();
            }));
    page.addView(header);
    u.gap(page, 16);
    // Keep record actions reachable when larger system fonts make the summary,
    // hint and filters taller than the available screen.
    LinearLayout body = u.column();
    summary = u.text("", 16, Ui.INK, true);
    u.pad(summary, 16);
    u.surface(summary, Ui.LIGHT, Color.TRANSPARENT, 12);
    body.addView(summary);
    u.gap(body, 12);
    TextView hint = u.text("上传完成表示已归档到电脑，不代表提交保司；视频暂不支持本地验真", 14, Color.rgb(145, 79, 0), false);
    u.pad(hint, 12);
    u.surface(hint, Color.rgb(255, 245, 224), Color.TRANSPARENT, 8);
    body.addView(hint);
    u.gap(body, 12);
    LinearLayout filters = u.row();
    String[] values = {"all", "pending", "failed"}, names = {"全部", "待处理", "异常"};
    TextView[] buttons = new TextView[3];
    for (int i = 0; i < 3; i++) {
      int index = i;
      buttons[i] = u.chip(names[i], i == 0, () -> {});
      buttons[i].setOnClickListener(
          v -> {
            filter = values[index];
            for (int j = 0; j < 3; j++) {
              boolean active = j == index;
              buttons[j].setTextColor(active ? Ui.ORANGE : Ui.INK);
              u.ripple(buttons[j], active ? Ui.PALE : Ui.LIGHT, active ? Ui.ORANGE : Ui.BORDER, 9);
            }
            refresh();
          });
      LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, -2, 1);
      if (i > 0) p.leftMargin = u.dp(8);
      filters.addView(buttons[i], p);
    }
    body.addView(filters);
    u.gap(body, 12);
    list = u.column();
    body.addView(list);
    page.addView(u.scroll(body), new LinearLayout.LayoutParams(-1, 0, 1));
    u.gap(page, 10);
    page.addView(
        u.button(
            "上传待处理资料",
            false,
            () -> {
              int count = 0;
              for (CaptureStore.Item item : a.store.recordItems())
                if (!item.demo()
                    && item.official()
                    && !item.state.equals("uploaded")
                    && !item.state.equals("uploading")
                    && !item.state.equals("rejected")
                    && !item.state.equals("awaiting_sdk")
                    && !item.state.equals("camera_error")
                    && !item.state.equals("cancelled")
                    && !item.state.equals("capture_failed")
                    && !item.state.equals("capturing")
                    && !item.state.equals("rendering")
                    && !deleting(item)) {
                  a.retry(item);
                  count++;
                }
              a.toast(count == 0 ? "没有待上传的资料" : "已加入上传队列，网络可用时继续");
              refresh();
            }));
    refresh();
    return page;
  }

  static boolean pending(CaptureStore.Item item) {
    return !item.demo()
        && (item.state.equals("delete_pending")
            || item.state.equals("queued")
            || item.state.equals("retry")
            || item.state.equals("uploading")
            || item.state.equals("verifying")
            || item.state.equals("awaiting_sdk")
            || item.state.equals("capturing")
            || item.state.equals("rendering"));
  }

  static boolean failed(CaptureStore.Item item) {
    return item.state.equals("delete_failed")
        || item.state.equals("failed")
        || item.state.equals("capture_failed")
        || item.state.equals("verification_error")
        || item.state.equals("rejected");
  }

  static boolean deleting(CaptureStore.Item item) {
    return item.state.equals("delete_pending") || item.state.equals("delete_failed")
        || item.state.equals("deleted");
  }

  static String transferDetail(CaptureStore.Item item) {
    if (deleting(item)) return "已停止上传";
    if (item.demo()) return "仅保存在手机";
    if (item.state.equals("uploaded") && item.upload.optInt("attempt") == 0) return "已归档到电脑";
    return UploadStatus.detail(item);
  }

  static String label(CaptureStore.Item item) {
    return switch (item.state) {
      case "delete_pending" -> "删除待同步";
      case "delete_failed" -> "删除未完成";
      case "deleted" -> "已删除";
      case "uploaded" -> item.official()
          ? (item.video() ? "视频已归档 · 未验真" : item.verified() ? "官方验真通过 · 已归档" : "已归档 · 未做本地验真")
          : "旧版照片 · 未官方验真";
      case "verifying" -> "等待官方验真";
      case "verification_error" -> "验真异常 · 未归档";
      case "rejected" -> "验真未通过 · 未归档";
      case "awaiting_sdk" -> "等待官方拍摄返回";
      case "camera_error" -> "相机未就绪";
      case "cancelled" -> "拍摄已退出，已有文件保留";
      case "uploading" -> UploadStatus.uploading(item.upload);
      case "retry" -> "等待自动重试 · 原片已保存";
      case "capturing", "rendering" -> "保存中";
      case "failed" -> "上传失败";
      case "capture_failed" -> "保存未完成";
      case "local" -> "仅存手机 · 示例";
      default -> "待上传";
    };
  }

  void tick() {
    StringBuilder next = new StringBuilder();
    List<CaptureStore.Item> items = a.store.recordItems();
    for (CaptureStore.Item item : items)
      next.append(item.id).append(item.state).append(item.error);
    if (!signature.equals(next.toString())) {
      signature = next.toString();
      refresh();
    } else {
      for (CaptureStore.Item item : items) {
        TextView label = statusLabels.get(item.id), detail = transferLabels.get(item.id);
        if (label != null) label.setText(label(item));
        if (detail != null) detail.setText(transferDetail(item));
        ProgressBar bar = progressBars.get(item.id);
        if (bar != null) updateProgress(bar, item);
      }
    }
  }

  void refresh() {
    if (list == null) return;
    List<CaptureStore.Item> items = a.store.recordItems();
    int uploaded = 0, queued = 0, errors = 0, local = 0;
    for (CaptureStore.Item item : items) {
      if (item.state.equals("uploaded") && item.official()) uploaded++;
      if (pending(item)) queued++;
      if (failed(item)) errors++;
      if (item.demo()) local++;
    }
    summary.setText(
        "拍摄记录 "
            + items.size()
            + " 条\n已归档 "
            + uploaded
            + "  ·  待处理 "
            + queued
            + "  ·  异常 "
            + errors
            + (local > 0 ? "\n示例照片 " + local + " 张，仅存手机" : ""));
    list.removeAllViews();
    statusLabels.clear();
    transferLabels.clear();
    progressBars.clear();
    String previous = "";
    int shown = 0, matching = 0;
    for (CaptureStore.Item item : items) {
      if (filter.equals("pending") && !pending(item) || filter.equals("failed") && !failed(item))
        continue;
      matching++;
      if (shown++ >= limit) continue;
      String group =
          CameraData.recordedDate(item)
              + " · "
              + item.meta.optString("project_name")
              + " · "
              + item.meta.optString("location");
      if (!group.equals(previous)) {
        u.gap(list, 8);
        list.addView(u.text(group, 13, Ui.MUTED, true));
        u.gap(list, 12);
        previous = group;
      }
      LinearLayout row = u.row();
      row.setPadding(0, u.dp(6), 0, u.dp(12));
      ImageView thumb = new ImageView(a);
      thumb.setScaleType(ImageView.ScaleType.CENTER_CROP);
      u.surface(thumb, Ui.LIGHT, Ui.BORDER, 7);
      thumb.setClipToOutline(true);
      if (a.store.thumbnail(item.id).isFile()) {
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inSampleSize = 2;
        thumb.setImageBitmap(
            BitmapFactory.decodeFile(a.store.thumbnail(item.id).getPath(), options));
      }
      row.addView(thumb, new LinearLayout.LayoutParams(u.dp(68), u.dp(82)));
      LinearLayout details = u.column();
      details.setPadding(u.dp(12), 0, u.dp(4), 0);
      details.addView(
          u.text((item.video() ? "▶ 视频 · " : "") + CameraData.label(item.meta.optString("photo_group")), 16, Ui.INK, true));
      u.gap(details, 7);
      details.addView(u.text(CameraData.recordedTime(item), 13, Ui.MUTED, false));
      u.gap(details, 7);
      int color =
          item.state.equals("uploaded") && item.official()
              ? Ui.GREEN
              : failed(item) ? Ui.RED : Ui.ORANGE;
      TextView stateLabel = u.text(label(item), 13, color, true);
      statusLabels.put(item.id, stateLabel);
      details.addView(stateLabel);
      row.addView(details, new LinearLayout.LayoutParams(0, -2, 1));
      if (item.official()
          && !deleting(item)
          && !item.state.equals("rejected")
          && (failed(item) || item.state.equals("retry"))) {
        TextView retry = u.button("重试", false, () -> a.retry(item));
        retry.setTextSize(13);
        row.addView(retry, new LinearLayout.LayoutParams(u.dp(64), -2));
      }
      row.setOnClickListener(v -> PhotoViewer.show(a, item));
      row.setContentDescription((item.video() ? "播放视频 " : "查看照片 ") + CameraData.time(item.meta));
      row.setFocusable(true);
      list.addView(row);
      {
        if (item.state.equals("uploading")) {
          ProgressBar progress = new ProgressBar(a, null, android.R.attr.progressBarStyleHorizontal);
          progress.setMax(100);
          updateProgress(progress, item);
          progressBars.put(item.id, progress);
          list.addView(progress, new LinearLayout.LayoutParams(-1, u.dp(6)));
        }
        LinearLayout transfer = u.row();
        TextView detail = u.text(transferDetail(item), 12, Ui.MUTED, false);
        transferLabels.put(item.id, detail);
        transfer.addView(detail, new LinearLayout.LayoutParams(0, -2, 1));
        TextView diagnostic = u.button("上传详情", false, () -> diagnostics(item.id));
        diagnostic.setTextSize(12);
        diagnostic.setTag("upload-details:" + item.id);
        transfer.addView(diagnostic, new LinearLayout.LayoutParams(u.dp(92), -2));
        String deleteText = item.state.equals("delete_pending") ? "待同步"
            : item.state.equals("delete_failed") ? "重试删除" : "删除";
        TextView remove = u.button(deleteText, false, () -> {
          if (item.state.equals("delete_failed")) retryDeletion(item.id);
          else delete(item.id);
        });
        remove.setTextSize(12);
        remove.setTextColor(Ui.RED);
        remove.setTag("delete:" + item.id);
        remove.setContentDescription(deleteText + (item.video() ? "视频 " : "照片 ")
            + item.meta.optString("case_number"));
        boolean saving = a.store.activeCaptures.contains(item.id)
            || item.state.equals("capturing") || item.state.equals("rendering")
            || item.state.equals("awaiting_sdk");
        remove.setEnabled(!saving && !item.state.equals("delete_pending"));
        remove.setAlpha(remove.isEnabled() ? 1f : .5f);
        LinearLayout.LayoutParams removeParams = new LinearLayout.LayoutParams(u.dp(76), -2);
        removeParams.leftMargin = u.dp(6);
        transfer.addView(remove, removeParams);
        list.addView(transfer);
      }
      if (!item.error.isEmpty()) {
        TextView message = u.text(item.error, 12, Ui.RED, false);
        message.setPadding(0, 0, 0, u.dp(12));
        list.addView(message);
      }
      u.divider(list);
    }
    if (matching == 0) {
      u.gap(list, 40);
      TextView empty =
          u.text(items.isEmpty() ? "还没有拍摄记录\n选好案件后开始拍摄" : "当前没有这类记录", 16, Ui.MUTED, false);
      empty.setGravity(Gravity.CENTER);
      list.addView(empty);
    } else if (matching > limit) {
      u.gap(list, 16);
      list.addView(
          u.button(
              "加载更多",
              false,
              () -> {
                limit += 60;
                refresh();
              }));
    }
    u.gap(list, 16);
  }

  private static void updateProgress(ProgressBar bar, CaptureStore.Item item) {
    boolean sending = "sending".equals(item.upload.optString("stage"));
    bar.setIndeterminate(!sending);
    bar.setProgress(UploadStatus.percent(item.upload));
    bar.setContentDescription(label(item));
  }

  void delete(String id) {
    CaptureStore.Item item = a.store.item(id);
    if (item == null || deleting(item)) return;
    if (a.store.activeCaptures.contains(id) || item.state.equals("capturing")
        || item.state.equals("rendering") || item.state.equals("awaiting_sdk")) {
      a.toast("资料正在保存，请完成后再删除");
      return;
    }
    LinearLayout content = u.column();
    u.pad(content, 20);
    ImageView thumbnail = new ImageView(a);
    thumbnail.setScaleType(ImageView.ScaleType.FIT_CENTER);
    thumbnail.setAdjustViewBounds(true);
    thumbnail.setContentDescription(item.video() ? "待删除视频缩略图" : "待删除照片缩略图");
    u.surface(thumbnail, Ui.LIGHT, Ui.BORDER, 8);
    if (a.store.thumbnail(id).isFile()) {
      BitmapFactory.Options options = new BitmapFactory.Options();
      options.inSampleSize = 2;
      thumbnail.setImageBitmap(BitmapFactory.decodeFile(a.store.thumbnail(id).getPath(), options));
    }
    content.addView(thumbnail, new LinearLayout.LayoutParams(-1, u.dp(160)));
    if (thumbnail.getDrawable() == null)
      content.addView(u.text("缩略图暂不可用，请核对案件信息", 13, Ui.MUTED, false));
    u.gap(content, 14);
    content.addView(u.text("案件号：" + item.meta.optString("case_number", "未填写"), 16, Ui.INK, true));
    u.gap(content, 8);
    content.addView(u.text(item.meta.optString("project_name") + " · "
        + item.meta.optString("location"), 14, Ui.MUTED, false));
    u.gap(content, 14);
    content.addView(u.text(item.demo() ? "删除这份手机上的示例资料。"
        : "确认后停止上传，并同步删除本次拍摄记录。后台保留原片和删除记录；其他来源共用的资料仍保留。\n断网时显示“删除待同步”，联网确认后完成。",
        14, Ui.INK, false));
    android.app.AlertDialog dialog = new android.app.AlertDialog.Builder(a)
        .setTitle(item.video() ? "删除视频" : "删除照片")
        .setView(u.scroll(content)).setNegativeButton("取消", null)
        .setPositiveButton("确认删除", (d, which) -> performDeletion(id, false)).create();
    dialog.setOnShowListener(d -> dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE).setTextColor(Ui.RED));
    dialog.show();
  }

  void retryDeletion(String id) {
    performDeletion(id, true);
  }

  private void performDeletion(String id, boolean retry) {
    a.io.execute(() -> {
      String result;
      try {
        if (retry) CaptureDeletion.retry(a, id);
        else CaptureDeletion.request(a, id);
        CaptureStore.Item current = a.store.item(id);
        result = current != null && current.state.equals("deleted") ? "已删除" : "已停止上传，删除待同步";
      } catch (Exception error) {
        result = "删除未完成：" + UploadWorker.safeMessage(error);
      }
      String message = result;
      a.runOnUiThread(() -> {
        if (a.isDestroyed()) return;
        if (a.recordsScreen == this) refresh();
        a.toast(message);
      });
    });
  }

  void diagnostics(String id) {
    CaptureStore.Item current = a.store.item(id);
    if (current == null) return;
    String text = UploadStatus.diagnostics(current);
    new android.app.AlertDialog.Builder(a).setTitle("上传详情").setMessage(text)
        .setPositiveButton("关闭", null)
        .setNeutralButton("复制诊断信息", (dialog, which) -> {
          android.content.ClipboardManager clipboard = a.getSystemService(android.content.ClipboardManager.class);
          clipboard.setPrimaryClip(android.content.ClipData.newPlainText("IDI 上传诊断", text));
          a.toast("诊断信息已复制");
        }).show();
  }
}
