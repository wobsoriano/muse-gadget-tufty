/* Host driver for one extra animation the Muse renderer has no mode for: a dance.
 *
 * The renderer only knows poses, so the dance is choreography over them: the
 * speaking pose (bob, arm gestures, shuffling feet) driven by a beat in place
 * of a voice, with the happy reaction (hop, arms up) on the second bar.
 * tools/avatar_sprites.py builds this against a muse_pixel.c and adds the sway.
 */
#include <math.h>
#include <stdio.h>
#include <stdint.h>

#include "muse_pixel.h"

#define N MUSE_PX_W
#define DT 0.04f
#define BEAT 0.5f       /* 120 BPM */
#define BEATS 8
#define WARMUP 1.5f

static uint16_t buf[N * N];

int main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : ".";
    int frames = (int)(BEATS * BEAT / DT + 0.5f);
    int warm = (int)(WARMUP / DT + 0.5f);
    muse_pixel_set_size(N);
    for (int i = -warm; i < frames; i++) {
        float rt = i * DT;
        float beat = rt / BEAT;
        float pulse = powf(fabsf(sinf(3.14159265f * beat)), 0.6f);
        float bar = beat - 4.0f;    /* the second bar is the happy one */
        float happy = 0;
        if (bar > 0) {
            happy = bar < 0.4f ? bar / 0.4f : (bar > 3.6f ? (4.0f - bar) / 0.4f : 1.0f);
        }
        muse_pose_t p = {
            .mode = MUSE_MODE_SPEAKING,
            .t = 500.0f + rt,
            .mode_t = 5.0f + rt,
            .level = i < 0 ? 0 : pulse,
            .happy = i < 0 ? 0 : happy,
        };
        muse_pixel_render(&p);
        if (i >= 0) {
            char path[512];
            muse_pixel_scale(buf, N, 0, N - 1, 0, N - 1);
            snprintf(path, sizeof(path), "%s/%03d.ppm", dir, i);
            FILE *f = fopen(path, "wb");
            fprintf(f, "P6 %d %d 255\n", N, N);
            for (int k = 0; k < N * N; k++) {
                uint16_t c = buf[k];
                uint8_t rgb[3] = { (uint8_t)(((c >> 11) & 31) * 255 / 31),
                                   (uint8_t)(((c >> 5) & 63) * 255 / 63),
                                   (uint8_t)((c & 31) * 255 / 31) };
                fwrite(rgb, 1, 3, f);
            }
            fclose(f);
        }
    }
    return 0;
}
