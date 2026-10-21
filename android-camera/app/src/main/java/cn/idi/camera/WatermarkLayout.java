package cn.idi.camera;

/** Geometry shared by rendering and JVM tests. Area is relative to photo pixels. */
final class WatermarkLayout {
  final float font, padding, lineHeight, width, height, x, y, fraction;

  WatermarkLayout(int imageWidth, int imageHeight, float widestEm, int lines) {
    float padEm = .55f, lineEm = 1.38f;
    float nominal =
        (float)
            Math.sqrt(
                imageWidth
                    * (double) imageHeight
                    * .06
                    / ((widestEm + padEm * 2) * (lines * lineEm + padEm * 2)));
    font =
        Math.min(
            nominal,
            Math.min(
                imageWidth * .48f / (widestEm + padEm * 2),
                imageHeight * .25f / (lines * lineEm + padEm * 2)));
    padding = font * padEm;
    lineHeight = font * lineEm;
    width = font * widestEm + 2 * padding;
    height = lines * lineHeight + 2 * padding;
    x = imageWidth * .025f;
    y = imageHeight - imageHeight * .025f - height;
    fraction = width * height / (imageWidth * (float) imageHeight);
  }
}
