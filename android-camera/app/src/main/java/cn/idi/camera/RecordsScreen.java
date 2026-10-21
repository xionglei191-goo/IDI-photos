package cn.idi.camera;

import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.util.List;

final class RecordsScreen {
  final MainActivity a;
  final Ui u;
  LinearLayout list;
  TextView summary;
  String filter = "all", signature = "";
  int limit = 60;

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
    summary = u.text("", 16, Ui.INK, true);
    u.pad(summary, 16);
    u.surface(summary, Ui.LIGHT, Color.TRANSPARENT, 12);
    page.addView(summary);
    u.gap(page, 12);
    TextView hint = u.text("上传完成表示已归档到电脑，不代表提交保司；视频暂不支持本地验真", 14, Color.rgb(145, 79, 0), false);
    u.pad(hint, 12);
    u.surface(hint, Color.rgb(255, 245, 224), Color.TRANSPARENT, 8);
    page.addView(hint);
    u.gap(page, 12);
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
    page.addView(filters);
    u.gap(page, 12);
    list = u.column();
    page.addView(u.scroll(list), new LinearLayout.LayoutParams(-1, 0, 1));
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
                    && !item.state.equals("rejected")
                    && !item.state.equals("awaiting_sdk")
                    && !item.state.equals("camera_error")
                    && !item.state.equals("cancelled")
                    && !item.state.equals("capture_failed")
                    && !item.state.equals("capturing")
                    && !item.state.equals("rendering")) {
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
        && (item.state.equals("queued")
            || item.state.equals("retry")
            || item.state.equals("uploading")
            || item.state.equals("verifying")
            || item.state.equals("awaiting_sdk")
            || item.state.equals("capturing")
            || item.state.equals("rendering"));
  }

  static boolean failed(CaptureStore.Item item) {
    return item.state.equals("failed")
        || item.state.equals("capture_failed")
        || item.state.equals("verification_error")
        || item.state.equals("rejected");
  }

  static String label(CaptureStore.Item item) {
    return switch (item.state) {
      case "uploaded" -> item.official()
          ? (item.video() ? "视频已归档 · 未验真" : item.verified() ? "官方验真通过 · 已归档" : "已归档 · 未做本地验真")
          : "旧版照片 · 未官方验真";
      case "verifying" -> "等待官方验真";
      case "verification_error" -> "验真异常 · 未归档";
      case "rejected" -> "验真未通过 · 未归档";
      case "awaiting_sdk" -> "等待官方拍摄返回";
      case "camera_error" -> "相机未就绪";
      case "cancelled" -> "拍摄已退出，已有文件保留";
      case "uploading" -> "上传中";
      case "capturing", "rendering" -> "保存中";
      case "failed" -> "上传失败";
      case "capture_failed" -> "保存未完成";
      case "local" -> "仅存手机 · 示例";
      default -> "待上传";
    };
  }

  void tick() {
    StringBuilder next = new StringBuilder();
    for (CaptureStore.Item item : a.store.recordItems())
      next.append(item.id).append(item.state).append(item.error);
    if (!signature.equals(next.toString())) {
      signature = next.toString();
      refresh();
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
      details.addView(u.text(label(item), 13, color, true));
      row.addView(details, new LinearLayout.LayoutParams(0, -2, 1));
      if (item.official()
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
}
