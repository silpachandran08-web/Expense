#!/bin/sh
# Downloads the two on-device models into the app's assets (kept out of git, about 10 MB).
set -e
dir="$(dirname "$0")/app/src/main/assets"
mkdir -p "$dir"
base=https://storage.googleapis.com/mediapipe-models
curl -fsSL -o "$dir/pose_landmarker_lite.task" "$base/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
curl -fsSL -o "$dir/mobilenet_v3_small.tflite" "$base/image_embedder/mobilenet_v3_small/float32/latest/mobilenet_v3_small.tflite"
