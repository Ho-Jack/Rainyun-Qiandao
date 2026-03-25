from math import cos, fabs, radians, sin
from pathlib import Path
from typing import BinaryIO, Literal, Union

import cv2
import numpy as np

ImageInput = Union[str, Path, bytes, BinaryIO, np.ndarray]
Rect = tuple[int, int, int, int]


def load_image(image_data: ImageInput) -> np.ndarray:
    if isinstance(image_data, np.ndarray):
        return image_data
    if isinstance(image_data, (str, Path)):
        return cv2.imread(str(image_data))
    if isinstance(image_data, bytes):
        return cv2.imdecode(np.frombuffer(image_data, np.uint8), cv2.IMREAD_COLOR)
    if hasattr(image_data, "read"):
        return cv2.imdecode(np.frombuffer(image_data.read(), np.uint8), cv2.IMREAD_COLOR)
    raise ValueError("Unsupported image input type")


def load_and_preprocess(image_data: ImageInput, threshold: int = 30) -> np.ndarray:
    image = load_image(image_data)
    if image is None:
        raise ValueError("Failed to load captcha image")
    return cv2.inRange(image, (0, 0, 0), (threshold, threshold, threshold))


def should_merge(rect1: Rect, rect2: Rect, overlap_threshold: float = 0.0) -> bool:
    x1, y1, w1, h1 = rect1
    x2, y2, w2, h2 = rect2

    x_left = max(x1, x2)
    y_top = max(y1, y2)
    x_right = min(x1 + w1, x2 + w2)
    y_bottom = min(y1 + h1, y2 + h2)
    if x_right <= x_left or y_bottom <= y_top:
        return False
    if overlap_threshold == 0:
        return True

    intersection_area = (x_right - x_left) * (y_bottom - y_top)
    min_area = min(w1 * h1, w2 * h2)
    return intersection_area > overlap_threshold * min_area


def merge_rectangles(rectangles: list[Rect], overlap_threshold: float = 0.0) -> list[Rect]:
    if not rectangles:
        return []

    rects = rectangles[:]
    changed = True
    while changed:
        changed = False
        merged_indices: set[int] = set()
        new_rects: list[Rect] = []

        for index, current in enumerate(rects):
            if index in merged_indices:
                continue

            merged_rect = current
            for candidate_index in range(index + 1, len(rects)):
                if candidate_index in merged_indices:
                    continue

                candidate = rects[candidate_index]
                if not should_merge(merged_rect, candidate, overlap_threshold):
                    continue

                x_min = min(merged_rect[0], candidate[0])
                y_min = min(merged_rect[1], candidate[1])
                x_max = max(merged_rect[0] + merged_rect[2], candidate[0] + candidate[2])
                y_max = max(merged_rect[1] + merged_rect[3], candidate[1] + candidate[3])
                merged_rect = (x_min, y_min, x_max - x_min, y_max - y_min)
                merged_indices.add(candidate_index)
                changed = True

            new_rects.append(merged_rect)

        rects = new_rects

    return rects


def merge_close_rectangles(rectangles: list[Rect], max_distance: int) -> list[Rect]:
    def rect_distance(rect1: Rect, rect2: Rect) -> float:
        x1, y1, w1, h1 = rect1
        x2, y2, w2, h2 = rect2
        x1_end, y1_end = x1 + w1, y1 + h1
        x2_end, y2_end = x2 + w2, y2 + h2

        if x1_end < x2:
            dx = x2 - x1_end
        elif x2_end < x1:
            dx = x1 - x2_end
        else:
            dx = 0

        if y1_end < y2:
            dy = y2 - y1_end
        elif y2_end < y1:
            dy = y1 - y2_end
        else:
            dy = 0

        return (dx ** 2 + dy ** 2) ** 0.5

    changed = True
    current_rectangles = rectangles[:]
    while changed and len(current_rectangles) > 1:
        changed = False
        merged = [False] * len(current_rectangles)
        new_rectangles: list[Rect] = []

        for index, rect in enumerate(current_rectangles):
            if merged[index]:
                continue

            merged_rect = rect
            for candidate_index in range(index + 1, len(current_rectangles)):
                if merged[candidate_index]:
                    continue

                candidate = current_rectangles[candidate_index]
                if rect_distance(merged_rect, candidate) > max_distance:
                    continue

                x = min(merged_rect[0], candidate[0])
                y = min(merged_rect[1], candidate[1])
                w = max(merged_rect[0] + merged_rect[2], candidate[0] + candidate[2]) - x
                h = max(merged_rect[1] + merged_rect[3], candidate[1] + candidate[3]) - y
                merged_rect = (x, y, w, h)
                merged[candidate_index] = True
                changed = True

            new_rectangles.append(merged_rect)
            merged[index] = True

        current_rectangles = new_rectangles

    return current_rectangles


def extract_black_regions(
    binary_image: np.ndarray,
    min_area: int = 100,
    merged: bool = True,
    merge_distance: int = 0,
    sort_mode: Literal["area-desc", "area-asc", "position-tl", "position-l"] = "area-desc",
) -> list[Rect]:
    contours, _ = cv2.findContours(binary_image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rectangles: list[Rect] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width * height >= min_area:
            rectangles.append((x, y, width, height))

    if merged:
        rectangles = merge_rectangles(rectangles)
    if merge_distance > 0 and len(rectangles) > 1:
        rectangles = merge_close_rectangles(rectangles, merge_distance)

    if sort_mode == "area-desc":
        rectangles.sort(key=lambda rect: rect[2] * rect[3], reverse=True)
    elif sort_mode == "area-asc":
        rectangles.sort(key=lambda rect: rect[2] * rect[3])
    elif sort_mode == "position-tl":
        rectangles.sort(key=lambda rect: (rect[1], rect[0]))
    elif sort_mode == "position-l":
        rectangles.sort(key=lambda rect: rect[0])

    return rectangles


def opencv_rotate(image: np.ndarray, angle: int, scale: float = 1.0) -> np.ndarray:
    height, width = image.shape[:2]
    center = (width / 2, height / 2)
    rotation_matrix = cv2.getRotationMatrix2D(center, angle, scale)
    new_height = int(width * fabs(sin(radians(angle))) + height * fabs(cos(radians(angle))))
    new_width = int(height * fabs(sin(radians(angle))) + width * fabs(cos(radians(angle))))
    rotation_matrix[0, 2] += (new_width - width) / 2
    rotation_matrix[1, 2] += (new_height - height) / 2
    return cv2.warpAffine(
        image,
        rotation_matrix,
        (new_width, new_height),
        borderValue=(0, 0, 0),
    )


def analyze_rotated_regions(sprite_mask: np.ndarray, sprite_black_regions: list[Rect]) -> list[dict]:
    rotation_data: list[dict] = []
    for x, y, width, height in sprite_black_regions:
        region_roi = sprite_mask[y : y + height, x : x + width]
        region_data = {"original_region": (x, y, width, height), "rotations": []}

        for angle in range(-45, 46):
            rotated_image = opencv_rotate(region_roi, -angle)
            rectangles = extract_black_regions(rotated_image, 0)
            if not rectangles:
                continue
            x_r, y_r, w_r, h_r = rectangles[0]
            region_data["rotations"].append(
                {
                    "angle": angle,
                    "rect": (x_r, y_r, w_r, h_r),
                    "rotated_image": rotated_image,
                }
            )

        rotation_data.append(region_data)

    return rotation_data


def binary_similarity(image1: np.ndarray, image2: np.ndarray) -> float:
    matching_pixels = np.count_nonzero((image1 > 127) == (image2 > 127))
    return (matching_pixels / image1.size) * 100


def brute_search(rotated_roi: np.ndarray, bg_roi: np.ndarray, bg_rect: Rect, width: int, height: int):
    max_similarity = -1.0
    best_rect = None
    bg_x, bg_y, _, bg_height = bg_rect
    _, _, bg_width, _ = bg_rect

    for offset_y in range(0, bg_height - height + 1):
        for offset_x in range(0, bg_width - width + 1):
            bg_sub_roi = bg_roi[offset_y : offset_y + height, offset_x : offset_x + width]
            similarity = binary_similarity(rotated_roi, bg_sub_roi)
            if similarity <= max_similarity:
                continue
            max_similarity = similarity
            best_rect = (bg_x + offset_x, bg_y + offset_y, width, height)

    return best_rect, max_similarity


def template_search(rotated_roi: np.ndarray, bg_roi: np.ndarray, bg_rect: Rect, width: int, height: int):
    bg_x, bg_y, bg_width, bg_height = bg_rect
    template = rotated_roi.astype(np.uint8)
    search_area = bg_roi[:bg_height, :bg_width].astype(np.uint8)
    result = cv2.matchTemplate(search_area, template, cv2.TM_CCOEFF_NORMED)
    _, max_value, _, max_position = cv2.minMaxLoc(result)
    return (bg_x + max_position[0], bg_y + max_position[1], width, height), max_value * 100


def match_sprite_to_background(
    bg_black_regions: list[Rect],
    preprocessed_bg: np.ndarray,
    rotation_data: list[dict],
    method: str = "template",
) -> list[dict]:
    matcher = {"template": template_search, "brute": brute_search}.get(method)
    all_matches: list[dict] = []

    for sprite_idx, sprite_data in enumerate(rotation_data):
        for bg_idx, bg_rect in enumerate(bg_black_regions):
            bg_x, bg_y, bg_width, bg_height = bg_rect
            bg_roi = preprocessed_bg[bg_y : bg_y + bg_height, bg_x : bg_x + bg_width]

            for rotation in sprite_data["rotations"]:
                rotated_image = rotation["rotated_image"]
                x_r, y_r, width, height = rotation["rect"]
                rotated_roi = rotated_image[y_r : y_r + height, x_r : x_r + width]

                if matcher is None:
                    max_width = max(width, bg_width)
                    max_height = max(height, bg_height)
                    sprite_resized = cv2.resize(rotated_roi, (max_width, max_height), interpolation=cv2.INTER_NEAREST)
                    bg_resized = cv2.resize(bg_roi, (max_width, max_height), interpolation=cv2.INTER_NEAREST)
                    similarity = binary_similarity(sprite_resized, bg_resized)
                    best_bg_rect = bg_rect
                else:
                    if height > bg_height or width > bg_width:
                        width = min(width, bg_width)
                        height = min(height, bg_height)
                        rotated_roi = cv2.resize(rotated_roi, (width, height), interpolation=cv2.INTER_NEAREST)
                    best_bg_rect, similarity = matcher(rotated_roi, bg_roi, bg_rect, width, height)

                all_matches.append(
                    {
                        "sprite_idx": sprite_idx,
                        "bg_idx": bg_idx,
                        "angle": rotation["angle"],
                        "similarity": similarity,
                        "sprite_rect": sprite_data["original_region"],
                        "bg_rect": best_bg_rect,
                        "rotated_sprite": rotated_roi,
                    }
                )

    all_matches.sort(key=lambda item: -item["similarity"])
    final_matches: list[dict] = []
    used_bg_regions: set[int] = set()
    used_sprites: set[int] = set()

    for match in all_matches:
        sprite_idx = match["sprite_idx"]
        bg_idx = match["bg_idx"]
        if sprite_idx in used_sprites:
            continue
        if matcher is None and bg_idx in used_bg_regions:
            continue

        final_matches.append(match)
        used_sprites.add(sprite_idx)
        used_bg_regions.add(bg_idx)

        if len(used_sprites) == len(rotation_data):
            break
        if matcher is None and len(used_bg_regions) == len(bg_black_regions):
            break

    final_matches.sort(key=lambda item: item.get("sprite_idx", float("inf")))
    return final_matches


def preprocess_mask(image: np.ndarray, scale_factor: Union[int, float] = 4, kernel_size: int = 2, iterations: int = 1):
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    image = cv2.dilate(image, kernel, iterations=iterations)
    height, width = image.shape
    image = cv2.resize(
        image,
        (int(width // scale_factor), int(height // scale_factor)),
        interpolation=cv2.INTER_AREA,
    )
    _, image = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY)
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_NEAREST)


def main(bg_data: ImageInput, sprite_data: ImageInput, match_method: str = "template") -> list[dict]:
    original_bg = load_image(bg_data)
    original_sprite = load_image(sprite_data)
    if original_bg is None or original_sprite is None:
        raise ValueError("Failed to load captcha inputs")

    height, width = original_sprite.shape[:2]
    original_sprite = cv2.resize(
        original_sprite,
        (int(width * 1.55), int(height * 1.55)),
        interpolation=cv2.INTER_NEAREST,
    )

    bg_mask = preprocess_mask(load_and_preprocess(original_bg, 25))
    sprite_mask = preprocess_mask(load_and_preprocess(original_sprite), 1)

    bg_black_regions = extract_black_regions(bg_mask, 50, merge_distance=5)[:10]
    sprite_black_regions = extract_black_regions(sprite_mask, sort_mode="position-l")
    rotation_data = analyze_rotated_regions(sprite_mask, sprite_black_regions)
    matches = match_sprite_to_background(bg_black_regions, bg_mask, rotation_data, match_method)

    for match in matches:
        sprite_rect = match.get("sprite_rect")
        if not sprite_rect:
            continue
        match["sprite_rect"] = tuple(int(value // 1.55) for value in sprite_rect)

    return matches


def convert_matches_to_positions(matches: list[dict]) -> list[tuple[float, float]]:
    positions: list[tuple[float, float]] = []
    for match in matches:
        x, y, width, height = match["bg_rect"]
        positions.append((x + width / 2, y + height / 2))
    return positions


def find_part_positions(bg_img: ImageInput, sprite_img: ImageInput, match_method: str = "template"):
    return convert_matches_to_positions(main(bg_img, sprite_img, match_method))
