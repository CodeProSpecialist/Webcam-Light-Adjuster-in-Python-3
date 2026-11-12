#!/usr/bin/env python3
"""Finding the car or the person, and aiming the correction at them.

The correction in webcam_enhance.py treats the whole frame alike. That is the
right default and it is not what you actually want at two in the morning: the
sky, the tarmac and the side of the house are not what the camera is there
for. The vehicle is. So this finds the vehicle - and anyone walking near it -
and gives those parts of the picture a second, stronger lift of their own.

WHAT DOES THE FINDING

An object detector, NanoDet-Plus-m from the OpenCV Model Zoo, run through
cv2.dnn. It is a real detector trained on COCO, not a shape heuristic: it
returns a class and a confidence, and of its eighty classes six are of any use
here - person, bicycle, car, motorcycle, bus, truck. The model is a 3.8 MB
data file in detector/ beside these scripts, Apache 2.0, and it needs no GPU.

The decoding of its output is written out below rather than imported from
anywhere, so nothing here runs downloaded code.

THE ORDER MATTERS, AND IT IS THE WHOLE TRICK

Detection runs on the CORRECTED frame, not the raw one. A detector looking at
a raw night frame sees an almost uniform black rectangle and finds nothing in
it - there is no contrast for it to work with. The same detector looking at
the frame after stacking, CLAHE and brain G has something to see. So the
pipeline is: correct the whole frame first, find things in the corrected
frame, then go back and give those regions more. The first correction is what
makes the finding possible.

WHEN IT WILL AND WILL NOT WORK

It works on what is actually visible. A car at 55 to 75 feet across a lit or
part-lit yard is a reasonable target; the same car on a moonless night with no
light at all is not, because the correction can only amplify what the sensor
recorded and if that is nothing then there is nothing to detect. Expect it to
find things through dusk, under a security light, and in anything approaching
usable light, and to find nothing on the darkest nights. When it finds
nothing, the ordinary whole-frame correction is what you get, which is exactly
what you would have had anyway.

It is also worth saying plainly: a detection is the detector's opinion, and on
a lifted night frame the picture it is looking at is itself partly guessed.
Treat a box as "something that looks like a car is probably there", not as
proof of one.

    python3 webcam_detect.py <image>        # find things and say what it found
    python3 webcam_detect.py <image> --save # and write an annotated copy
"""

import os
import sys

# The six COCO classes worth anything here, and what to call each one.
# NanoDet is trained on all eighty; the rest are ignored rather than reported,
# because a "potted plant" in a dark yard is a false positive and nothing else.
COCO_CLASSES = [
    'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train',
    'truck', 'boat', 'traffic light', 'fire hydrant', 'stop sign',
    'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
    'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag',
    'tie', 'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball', 'kite',
    'baseball bat', 'baseball glove', 'skateboard', 'surfboard',
    'tennis racket', 'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon',
    'bowl', 'banana', 'apple', 'sandwich', 'orange', 'broccoli', 'carrot',
    'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch', 'potted plant',
    'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote',
    'keyboard', 'cell phone', 'microwave', 'oven', 'toaster', 'sink',
    'refrigerator', 'book', 'clock', 'vase', 'scissors', 'teddy bear',
    'hair drier', 'toothbrush',
]

WANTED = {
    'person': 'person',
    'bicycle': 'vehicle',
    'car': 'vehicle',
    'motorcycle': 'vehicle',
    'bus': 'vehicle',
    'truck': 'vehicle',
}

MODEL_NAME = 'object_detection_nanodet_2022nov.onnx'

# The model's own numbers. NanoDet-Plus-m takes a 416 by 416 BGR image,
# normalized with these, and predicts at four strides with a distribution of
# eight bins per box side.
INPUT_SIZE = 416
REG_MAX = 7
NUM_CLASSES = 80
# 4 sides, each a distribution over REG_MAX + 1 bins.
BOX_CHANNELS = 4 * (REG_MAX + 1)
MEAN = (103.53, 116.28, 123.675)
STD = (57.375, 57.12, 58.395)

# Confidence to believe. Lower than a daylight application would use, because
# a lifted night frame is a noisy thing to look at and a missed car helps
# nobody; the cost of a false box is a bit of extra lift on a patch of fence.
THRESHOLD = float(os.environ.get('WEBCAM_DETECT_THRESHOLD', 0.32))
IOU_THRESHOLD = 0.6

# How much extra to give what was found, and how far the edge of that extra
# is feathered so the boost does not show up as a rectangle drawn on the
# picture. The feather is in pixels.
BOOST_GAIN = float(os.environ.get('WEBCAM_BOOST', 1.45))
BOOST_FEATHER = 25
BOOST_CLAHE_CLIP = 3.0


# Every handler in here says what went wrong through this, so an OpenCV
# failure shows up in the window's terminal pane instead of only showing up as
# a frame that quietly did not get corrected.
try:
    import webcam_errors
except ImportError:                                               # noqa: BLE001
    webcam_errors = None


def _oops(where, problem=None, detail='', once_key=''):
    """Report a failure and carry on. A no-op if webcam_errors.py was
    deleted, because the correction working matters more than the log."""
    if webcam_errors is None:
        return
    if once_key:
        webcam_errors.once(once_key, where, detail or str(problem))
    else:
        webcam_errors.report(where, problem, detail=detail)


def _cv2():
    if _cv2.module is None:
        try:
            import cv2
            import numpy
        except ImportError:
            _cv2.module = False
        else:
            _cv2.module = (cv2, numpy)
    return _cv2.module or None


_cv2.module = None


def model_dirs():
    """Where to look for the detector, in order."""
    here = os.path.dirname(os.path.abspath(__file__))
    found = []
    if os.environ.get('WEBCAM_DETECTOR_DIR'):
        found.append(os.environ['WEBCAM_DETECTOR_DIR'])
    found.append(os.path.join(os.path.expanduser('~'), '.webcam-models', 'detector'))
    found.append(os.path.join(here, 'detector'))
    return found


def model_path():
    for folder in model_dirs():
        path = os.path.join(folder, MODEL_NAME)
        if os.path.isfile(path):
            return path
    return None


class Detector:
    """NanoDet through cv2.dnn, with the output decoded here.

    The net is read once and kept. Everything returns empty rather than
    raising: this is an extra on top of a correction that has to keep working
    whatever happens here.
    """

    def __init__(self, threshold=THRESHOLD):
        self.threshold = threshold
        self.net = None
        self.why = ''
        self.anchors = None
        self.last = []

    # ---- the net ----

    def ready(self):
        """Load the net if it is not loaded. True when there is one."""
        if self.net is not None:
            return True
        tools = _cv2()
        if not tools:
            self.why = 'OpenCV is not installed, so nothing can be detected'
            return False
        cv2, numpy = tools
        path = model_path()
        if not path:
            self.why = (f'{MODEL_NAME} was not found in '
                        + ', '.join(model_dirs()))
            return False
        try:
            self.net = cv2.dnn.readNet(path)
        except Exception as problem:                          # noqa: BLE001
            self.net = None
            self.why = f'could not load the detector: {problem}'
            _oops('detector.load', problem, detail=f'model {path}')
            return False

        # Anchors are built per stride on demand rather than from a fixed
        # list, because how many strides a NanoDet export has is a property of
        # that export - this one has three, other builds have four - and a
        # hard-coded list silently mismatches the model's own output.
        self.anchors = {}
        self.why = ''
        return True

    def anchors_for(self, count):
        """The anchor centres for a feature map with this many cells.

        A square map of `count` cells over a 416 pixel input means a stride of
        416 divided by the side length, so the stride is worked out from the
        model's own output rather than assumed.
        """
        _cv2_mod, numpy = _cv2()
        if count in self.anchors:
            return self.anchors[count]
        side = int(round(count ** 0.5))
        if side * side != count or side == 0:
            return None
        stride = INPUT_SIZE / side
        shift = numpy.arange(side) * stride + 0.5 * (stride - 1)
        grid_x, grid_y = numpy.meshgrid(shift, shift)
        anchors = numpy.column_stack((grid_x.ravel(), grid_y.ravel()))
        self.anchors[count] = (anchors, stride)
        return self.anchors[count]

    # ---- detection ----

    def detect(self, image):
        """Find people and vehicles in a BGR image. Returns a list of dicts
        with x, y, w, h in the image's own pixels, plus label, kind, score.
        """
        if not self.ready():
            return []
        cv2, numpy = _cv2()
        try:
            height, width = image.shape[:2]
            resized = cv2.resize(image, (INPUT_SIZE, INPUT_SIZE))
            work = resized.astype(numpy.float32)
            work = (work - numpy.array(MEAN, numpy.float32)) / \
                numpy.array(STD, numpy.float32)
            self.net.setInput(cv2.dnn.blobFromImage(work))
            outs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        except Exception as problem:                          # noqa: BLE001
            self.why = f'detection failed: {problem}'
            _oops('detector.detect', problem,
                  detail='the net ran but the forward pass threw')
            return []

        # Sort the outputs out by what they are rather than by the order they
        # came back in. The order of getUnconnectedOutLayersNames is not
        # promised to be anything in particular - this export returns all
        # three score maps and then all three box maps, and assuming they
        # alternate gets them paired wrongly and silently. A map whose last
        # dimension is the class count is scores; one with four sides times
        # eight bins is boxes; and the two are paired by how many cells they
        # have, which is what says they came from the same stride.
        score_maps, box_maps = {}, {}
        for out in outs:
            array = out[0] if out.ndim == 3 else out
            if array.ndim != 2:
                continue
            cells, channels = array.shape
            if channels == BOX_CHANNELS:
                box_maps[cells] = array
            else:
                score_maps[cells] = array

        all_boxes, all_scores = [], []
        for cells in sorted(score_maps, reverse=True):
            scores = score_maps[cells]
            boxes = box_maps.get(cells)
            if boxes is None:
                continue
            pair = self.anchors_for(cells)
            if pair is None:
                continue
            anchors, stride = pair

            # Each side of each box is predicted as a distribution over eight
            # bins rather than a single number; the distance is its expected
            # value. Softmax, then a dot with 0..7, then scale by the stride.
            raw = boxes.reshape(-1, REG_MAX + 1)
            raw = raw - raw.max(axis=1, keepdims=True)         # stable softmax
            weights = numpy.exp(raw)
            weights /= weights.sum(axis=1, keepdims=True)
            distance = weights.dot(numpy.arange(REG_MAX + 1)).reshape(-1, 4)
            distance *= stride

            left = anchors[:, 0] - distance[:, 0]
            top = anchors[:, 1] - distance[:, 1]
            right = anchors[:, 0] + distance[:, 2]
            bottom = anchors[:, 1] + distance[:, 3]
            numpy.clip(left, 0, INPUT_SIZE, out=left)
            numpy.clip(top, 0, INPUT_SIZE, out=top)
            numpy.clip(right, 0, INPUT_SIZE, out=right)
            numpy.clip(bottom, 0, INPUT_SIZE, out=bottom)

            all_boxes.append(numpy.column_stack([left, top,
                                                 right - left, bottom - top]))
            all_scores.append(scores)

        boxes = numpy.concatenate(all_boxes, axis=0)
        scores = numpy.concatenate(all_scores, axis=0)
        class_ids = numpy.argmax(scores, axis=1)
        confidences = numpy.max(scores, axis=1)

        # Only the six classes that matter, before non-maximum suppression, so
        # a confident sofa cannot suppress a faint car.
        keep = numpy.array([COCO_CLASSES[i] in WANTED if i < len(COCO_CLASSES)
                            else False for i in class_ids])
        keep &= confidences >= self.threshold
        if not keep.any():
            self.last = []
            return []
        boxes, confidences, class_ids = (boxes[keep], confidences[keep],
                                         class_ids[keep])

        try:
            indices = cv2.dnn.NMSBoxes(boxes.tolist(), confidences.tolist(),
                                       self.threshold, IOU_THRESHOLD)
        except Exception as problem:                          # noqa: BLE001
            self.last = []
            _oops('detector.nms', problem,
                  detail='OpenCV NMSBoxes threw - no boxes kept this pass')
            return []
        indices = numpy.array(indices).reshape(-1)

        scale_x, scale_y = width / INPUT_SIZE, height / INPUT_SIZE
        found = []
        for i in indices:
            label = COCO_CLASSES[int(class_ids[i])]
            x, y, w, h = boxes[i]
            found.append({
                'x': max(0, int(round(x * scale_x))),
                'y': max(0, int(round(y * scale_y))),
                'w': min(width, int(round(w * scale_x))),
                'h': min(height, int(round(h * scale_y))),
                'label': label,
                'kind': WANTED[label],
                'score': round(float(confidences[i]), 3),
            })
        found.sort(key=lambda box: box['score'], reverse=True)
        self.last = found
        return found

    # ---- aiming the correction at what was found ----

    def boost(self, image, boxes, gain=BOOST_GAIN):
        """Give the found regions a second, stronger lift.

        Two things happen inside each box and nowhere else: a gain, and a
        CLAHE of its own run on that region alone - which is the part that
        actually pulls detail out, because a CLAHE over the whole frame has to
        set its tiles by the whole frame, and one run on the car alone sets
        them by the car.

        The edge is feathered over a couple of dozen pixels. Without that the
        boost shows up as a bright rectangle sitting on the picture, which
        looks like damage and hides the very edge of the thing being looked
        at.
        """
        if not boxes:
            return image
        tools = _cv2()
        if not tools:
            return image
        cv2, numpy = tools
        try:
            height, width = image.shape[:2]
            mask = numpy.zeros((height, width), numpy.float32)
            lifted = image.copy()

            for box in boxes:
                x, y = box['x'], box['y']
                w, h = max(1, box['w']), max(1, box['h'])
                x2, y2 = min(width, x + w), min(height, y + h)
                if x2 <= x or y2 <= y:
                    continue
                mask[y:y2, x:x2] = 1.0

                region = lifted[y:y2, x:x2]
                lab = cv2.cvtColor(region, cv2.COLOR_BGR2LAB)
                light, green_red, blue_yellow = cv2.split(lab)
                tiles = (max(2, min(8, (x2 - x) // 24)),
                         max(2, min(8, (y2 - y) // 24)))
                clahe = cv2.createCLAHE(clipLimit=BOOST_CLAHE_CLIP,
                                        tileGridSize=tiles)
                region = cv2.cvtColor(
                    cv2.merge((clahe.apply(light), green_red, blue_yellow)),
                    cv2.COLOR_LAB2BGR)
                lifted[y:y2, x:x2] = region

            lifted = numpy.clip(lifted.astype(numpy.float32) * gain,
                                0, 255).astype(numpy.uint8)
            # Feather, so the boost fades in rather than appearing at an edge.
            blur = max(3, BOOST_FEATHER | 1)
            mask = cv2.GaussianBlur(mask, (blur, blur), 0)[..., None]
            out = image.astype(numpy.float32) * (1 - mask) + \
                lifted.astype(numpy.float32) * mask
            return numpy.clip(out, 0, 255).astype(numpy.uint8)
        except Exception as problem:                          # noqa: BLE001
            _oops('detector.boost', problem,
                  detail='returning the frame unboosted')
            return image

    def draw(self, image, boxes):
        """A copy with the boxes drawn on, for looking at. Never the copy that
        gets kept as the corrected still - a box drawn across a number plate
        is worse than no box."""
        tools = _cv2()
        if not tools or not boxes:
            return image
        cv2, _numpy = tools
        out = image.copy()
        for box in boxes:
            # BGR, not RGB: (90, 180, 250) reads as orange on the frame,
            # which is what this drew before. Blue for a vehicle, green for a
            # person, matching the legend under the pictures in the window.
            colour = (90, 220, 90) if box['kind'] == 'person' else (250, 180, 90)
            cv2.rectangle(out, (box['x'], box['y']),
                          (box['x'] + box['w'], box['y'] + box['h']), colour, 2)
            text = f"{box['label']} {box['score']:.2f}"
            cv2.putText(out, text, (box['x'], max(14, box['y'] - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
        return out

    def crops(self, image, boxes, folder, keep=40):
        """Write a close crop of each thing found.

        This is the part that gets looked at in the morning: a folder of the
        car, rather than a folder of the yard with the car in the corner of
        it. A margin is left around each box because the detector's idea of
        where a car ends is tighter than a person's.
        """
        tools = _cv2()
        if not tools or not boxes or not folder:
            return []
        cv2, _numpy = tools
        import glob
        import time

        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return []
        height, width = image.shape[:2]
        stamp = time.strftime('%Y%m%d-%H%M%S')
        written = []
        for index, box in enumerate(boxes):
            pad_x, pad_y = int(box['w'] * 0.18), int(box['h'] * 0.18)
            x = max(0, box['x'] - pad_x)
            y = max(0, box['y'] - pad_y)
            x2 = min(width, box['x'] + box['w'] + pad_x)
            y2 = min(height, box['y'] + box['h'] + pad_y)
            if x2 - x < 8 or y2 - y < 8:
                continue
            name = (f'{stamp}-{index + 1}-{box["label"]}-'
                    f'{int(box["score"] * 100)}.jpg')
            path = os.path.join(folder, name)
            try:
                cv2.imwrite(path, image[y:y2, x:x2],
                            [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                written.append(path)
            except Exception as problem:                      # noqa: BLE001
                _oops('detector.crops', problem, detail='one crop not written')
                continue

        # Keep the folder bounded, the way the snapshots are kept.
        shots = sorted(glob.glob(os.path.join(folder, '*.jpg')),
                       key=os.path.getmtime)
        for old in shots[:max(0, len(shots) - keep)]:
            try:
                os.remove(old)
            except OSError:
                pass
        return written

    def summary(self, boxes=None):
        """One line naming what was found, for a status line."""
        boxes = self.last if boxes is None else boxes
        if not boxes:
            return 'nothing found' if self.ready() else self.why
        counted = {}
        for box in boxes:
            counted[box['label']] = counted.get(box['label'], 0) + 1
        parts = [f'{count} {label}{"s" if count > 1 else ""}'
                 for label, count in sorted(counted.items())]
        best = max(box['score'] for box in boxes)
        return ', '.join(parts) + f' (best {best:.2f})'


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-2].strip())
        return 1
    tools = _cv2()
    if not tools:
        print('OpenCV is not installed', file=sys.stderr)
        return 1
    cv2, _numpy = tools

    path = sys.argv[1]
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        print(f'could not read {path}', file=sys.stderr)
        return 1

    detector = Detector()
    if not detector.ready():
        print(detector.why, file=sys.stderr)
        return 1
    boxes = detector.detect(image)
    print(detector.summary(boxes))
    for box in boxes:
        print(f"  {box['label']:<12} {box['score']:.2f}  "
              f"at {box['x']},{box['y']} {box['w']}x{box['h']}")
    if '--save' in sys.argv:
        out = os.path.splitext(path)[0] + '-detected.jpg'
        cv2.imwrite(out, detector.draw(image, boxes))
        print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
