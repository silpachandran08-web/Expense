# Personal Outfit (Android)

Learns your style from 3 to 5 full-length photos of you, then scores any dress photo for you, with reasons.
Everything runs on the phone (no internet permission).

- **Your style:** each photo becomes an image embedding (MediaPipe, MobileNet V3), cropped to your body.
- **Body shape:** shoulder-to-hip ratio from MediaPipe Pose → inverted triangle / balanced / pear, with a tip.
- **Colors:** your usual outfit colors and your skin undertone (warm / cool, from a cheek sample).
- **Score:** 70% style similarity + 30% color fit. 👍 / 👎 on a dress adds it to your liked / disliked list,
  so later scores learn from it.
- **Share sheet:** share a dress photo from any app to Personal Outfit to score it.

Code: `Style.kt` (all scoring, unit-tested in `StyleTest.kt`), `Vision.kt` (the two models), `MainActivity.kt` (the screen).

Build: `./models.sh` once (downloads the models into assets), then `gradle -p outfit assembleDebug`, or open
`outfit/` in Android Studio. CI (`.github/workflows/outfit.yml`) builds and tests it on every push.
