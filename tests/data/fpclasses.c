/* Regression data: one function whose values are XMM (class 6 in Color) and one whose values are x87
   (Color's x87 class on 14.00+, FPColor on 13.10). Compile with /arch:SSE2. */
#include <emmintrin.h>
__m128 g(__m128);
__m128 f(const float *p, int n) { __m128 s = _mm_setzero_ps(), t = _mm_set1_ps(1.0f); int i;
  for (i = 0; i < n; i++) { __m128 v = _mm_loadu_ps(p + 4 * i); s = _mm_add_ps(s, _mm_mul_ps(v, t)); t = _mm_sub_ps(t, v); }
  return _mm_add_ps(s, t); }
float h(float x, float y, float z) { float u = x * y + z, v = x - y * z; return u * v + u / v; }
