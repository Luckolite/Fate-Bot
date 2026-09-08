package xyz.fatebot.status;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.os.SystemClock;
import android.view.View;

/** A lightweight, lifecycle-aware decorative gear with no animator allocation. */
final class GearView extends View {
    private static final int TEETH = 12;
    private static final long DEFAULT_PERIOD_MS = 10_000L;

    private final Paint fillPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint linePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint hubPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path gearPath = new Path();
    private long periodMs = DEFAULT_PERIOD_MS;
    private int direction = 1;
    private boolean running;
    private float centerX;
    private float centerY;
    private float hubRadius;
    private float ringRadius;

    GearView(Context context) {
        this(context, 0x66FFFFFF, 0xCCFFFFFF);
    }

    GearView(Context context, int accent, int secondary) {
        super(context);
        fillPaint.setColor(accent);
        fillPaint.setStyle(Paint.Style.FILL);
        fillPaint.setAlpha(92);
        linePaint.setColor(secondary);
        linePaint.setStyle(Paint.Style.STROKE);
        linePaint.setStrokeWidth(context.getResources().getDisplayMetrics().density);
        linePaint.setAlpha(190);
        hubPaint.setColor(secondary);
        hubPaint.setStyle(Paint.Style.FILL);
        hubPaint.setAlpha(180);
        setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);
        setClickable(false);
        setFocusable(false);
    }

    GearView reverse() {
        direction = -1;
        return this;
    }

    GearView period(long milliseconds) {
        periodMs = Math.max(2_500L, milliseconds);
        return this;
    }

    @Override
    protected void onSizeChanged(int width, int height, int oldWidth, int oldHeight) {
        super.onSizeChanged(width, height, oldWidth, oldHeight);
        centerX = width / 2f;
        centerY = height / 2f;
        float outerRadius = Math.max(0f, Math.min(width, height) / 2f - linePaint.getStrokeWidth());
        float rootRadius = outerRadius * 0.74f;
        float shoulderRadius = outerRadius * 0.86f;
        hubRadius = outerRadius * 0.18f;
        ringRadius = outerRadius * 0.48f;
        gearPath.reset();
        int points = TEETH * 4;
        for (int point = 0; point < points; point++) {
            float radius;
            switch (point % 4) {
                case 0:
                case 3:
                    radius = rootRadius;
                    break;
                default:
                    radius = point % 4 == 1 ? shoulderRadius : outerRadius;
                    break;
            }
            double angle = -Math.PI / 2d + point * (Math.PI * 2d / points);
            float x = centerX + (float) Math.cos(angle) * radius;
            float y = centerY + (float) Math.sin(angle) * radius;
            if (point == 0) gearPath.moveTo(x, y); else gearPath.lineTo(x, y);
        }
        gearPath.close();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        long now = SystemClock.uptimeMillis();
        float angle = direction * ((now % periodMs) / (float) periodMs) * 360f;
        canvas.save();
        canvas.rotate(angle, centerX, centerY);
        canvas.drawPath(gearPath, fillPaint);
        canvas.drawCircle(centerX, centerY, ringRadius, linePaint);
        canvas.drawCircle(centerX, centerY, hubRadius, hubPaint);
        canvas.restore();
        if (running && isShown()) postInvalidateOnAnimation();
    }

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        running = true;
        postInvalidateOnAnimation();
    }

    @Override
    protected void onDetachedFromWindow() {
        running = false;
        super.onDetachedFromWindow();
    }

    @Override
    protected void onVisibilityChanged(View changedView, int visibility) {
        super.onVisibilityChanged(changedView, visibility);
        if (visibility == VISIBLE && running) postInvalidateOnAnimation();
    }
}
