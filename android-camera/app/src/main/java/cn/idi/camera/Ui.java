package cn.idi.camera;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

final class Ui {
  static final int INK = Color.rgb(23, 33, 43),
      MUTED = Color.rgb(88, 100, 114),
      ORANGE = Color.rgb(201, 64, 22);
  static final int LIGHT = Color.rgb(247, 248, 250),
      BORDER = Color.rgb(222, 227, 233),
      DARK = Color.rgb(15, 24, 31);
  static final int PALE = Color.rgb(255, 242, 234),
      GREEN = Color.rgb(34, 120, 80),
      RED = Color.rgb(185, 36, 31);
  final Context c;

  Ui(Context c) {
    this.c = c;
  }

  int dp(float value) {
    return Math.round(value * c.getResources().getDisplayMetrics().density);
  }

  LinearLayout column() {
    LinearLayout v = new LinearLayout(c);
    v.setOrientation(LinearLayout.VERTICAL);
    return v;
  }

  LinearLayout row() {
    LinearLayout v = new LinearLayout(c);
    v.setOrientation(LinearLayout.HORIZONTAL);
    v.setGravity(Gravity.CENTER_VERTICAL);
    return v;
  }

  void pad(View v, int amount) {
    v.setPadding(dp(amount), dp(amount), dp(amount), dp(amount));
  }

  TextView text(String value, float size, int color, boolean bold) {
    TextView t = new TextView(c);
    t.setText(value);
    t.setTextSize(size);
    t.setTextColor(color);
    t.setIncludeFontPadding(false);
    t.setTypeface(Typeface.create("sans-serif", bold ? Typeface.BOLD : Typeface.NORMAL));
    t.setLineSpacing(dp(3), 1);
    return t;
  }

  GradientDrawable shape(int fill, int stroke, int radius) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(fill);
    d.setCornerRadius(dp(radius));
    if (stroke != Color.TRANSPARENT) d.setStroke(dp(1), stroke);
    return d;
  }

  void surface(View v, int fill, int stroke, int radius) {
    v.setBackground(shape(fill, stroke, radius));
  }

  void ripple(View v, int fill, int stroke, int radius) {
    v.setBackground(
        new RippleDrawable(
            ColorStateList.valueOf(Color.argb(35, 120, 130, 140)),
            shape(fill, stroke, radius),
            null));
  }

  TextView button(String label, boolean primary, Runnable action) {
    TextView b = text(label, 16, primary ? Color.WHITE : INK, true);
    b.setGravity(Gravity.CENTER);
    b.setMinHeight(dp(50));
    b.setPadding(dp(12), dp(10), dp(12), dp(10));
    ripple(b, primary ? ORANGE : Color.WHITE, primary ? Color.TRANSPARENT : BORDER, 10);
    b.setFocusable(true);
    b.setOnClickListener(v -> action.run());
    b.setContentDescription(label);
    return b;
  }

  TextView chip(String label, boolean selected, Runnable action) {
    TextView b = button(label, false, action);
    b.setTextSize(14);
    b.setTypeface(Typeface.DEFAULT, Typeface.NORMAL);
    b.setTextColor(selected ? ORANGE : INK);
    ripple(b, selected ? PALE : LIGHT, selected ? ORANGE : BORDER, 9);
    return b;
  }

  View icon(String glyph, String label, int color, Runnable action) {
    Glyph v = new Glyph(c, glyph, color);
    v.setContentDescription(label);
    v.setFocusable(true);
    ripple(v, Color.TRANSPARENT, Color.TRANSPARENT, 10);
    v.setOnClickListener(x -> action.run());
    v.setLayoutParams(new LinearLayout.LayoutParams(dp(48), dp(48)));
    return v;
  }

  EditText input(String value, String hint, int max) {
    EditText e = new EditText(c);
    e.setText(value);
    e.setHint(hint);
    e.setTextSize(16);
    e.setTextColor(INK);
    e.setHintTextColor(MUTED);
    e.setPadding(dp(12), dp(12), dp(12), dp(12));
    e.setMinHeight(dp(52));
    surface(e, Color.WHITE, BORDER, 9);
    e.setFilters(new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(max)});
    e.setContentDescription(hint);
    return e;
  }

  ScrollView scroll(View child) {
    ScrollView s = new ScrollView(c);
    s.setFillViewport(false);
    s.setClipToPadding(false);
    s.addView(child);
    return s;
  }

  void gap(LinearLayout parent, int height) {
    View v = new View(c);
    parent.addView(v, new LinearLayout.LayoutParams(1, dp(height)));
  }

  void divider(LinearLayout parent) {
    View v = new View(c);
    v.setBackgroundColor(BORDER);
    parent.addView(v, new LinearLayout.LayoutParams(-1, dp(1)));
  }

  void weight(LinearLayout row, View child) {
    row.addView(child, new LinearLayout.LayoutParams(0, -2, 1));
  }

  static final class Glyph extends View {
    private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final String name;
    private final int color;

    Glyph(Context c, String name, int color) {
      super(c);
      this.name = name;
      this.color = color;
    }

    @Override
    protected void onDraw(Canvas canvas) {
      super.onDraw(canvas);
      float unit = Math.min(getWidth(), getHeight()) / 48f;
      canvas.save();
      canvas.translate((getWidth() - 24 * unit) / 2, (getHeight() - 24 * unit) / 2);
      canvas.scale(unit, unit);
      p.setColor(color);
      p.setStrokeWidth(1.8f);
      p.setStyle(Paint.Style.STROKE);
      p.setStrokeJoin(Paint.Join.ROUND);
      p.setStrokeCap(Paint.Cap.ROUND);
      Path path = new Path();
      switch (name) {
        case "camera":
          canvas.drawRoundRect(new RectF(2, 6, 22, 21), 3, 3, p);
          canvas.drawCircle(12, 13, 4, p);
          path.moveTo(7, 6);
          path.lineTo(9, 3);
          path.lineTo(15, 3);
          path.lineTo(17, 6);
          canvas.drawPath(path, p);
          break;
        case "cases":
          path.moveTo(3, 10);
          path.lineTo(12, 3);
          path.lineTo(21, 10);
          path.moveTo(5, 9);
          path.lineTo(5, 21);
          path.lineTo(19, 21);
          path.lineTo(19, 9);
          path.moveTo(9, 21);
          path.lineTo(9, 14);
          path.lineTo(15, 14);
          path.lineTo(15, 21);
          canvas.drawPath(path, p);
          break;
        case "records":
          canvas.drawRoundRect(new RectF(5, 3, 19, 22), 2, 2, p);
          for (int y = 8; y <= 16; y += 4) canvas.drawLine(8, y, 16, y, p);
          break;
        case "back":
          path.moveTo(15, 4);
          path.lineTo(7, 12);
          path.lineTo(15, 20);
          canvas.drawPath(path, p);
          break;
        case "close":
          canvas.drawLine(6, 6, 18, 18, p);
          canvas.drawLine(18, 6, 6, 18, p);
          break;
        case "settings":
          canvas.drawCircle(12, 12, 8, p);
          canvas.drawCircle(12, 12, 3, p);
          for (int i = 0; i < 8; i++) {
            canvas.save();
            canvas.rotate(i * 45, 12, 12);
            canvas.drawLine(12, 1, 12, 4, p);
            canvas.restore();
          }
          break;
        case "flash":
          path.moveTo(14, 2);
          path.lineTo(5, 14);
          path.lineTo(11, 14);
          path.lineTo(10, 23);
          path.lineTo(20, 10);
          path.lineTo(13, 10);
          path.close();
          canvas.drawPath(path, p);
          break;
        case "refresh":
          canvas.drawArc(new RectF(4, 4, 20, 20), 45, 290, false, p);
          path.moveTo(18, 2);
          path.lineTo(20, 7);
          path.lineTo(15, 7);
          canvas.drawPath(path, p);
          break;
        case "check":
          path.moveTo(4, 12);
          path.lineTo(10, 18);
          path.lineTo(21, 6);
          canvas.drawPath(path, p);
          break;
        default:
          canvas.drawCircle(12, 12, 8, p);
          canvas.drawLine(12, 7, 12, 13, p);
          canvas.drawPoint(12, 17, p);
      }
      canvas.restore();
    }
  }
}
