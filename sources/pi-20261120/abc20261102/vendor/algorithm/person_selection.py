"""Optional output filters; zero max_people means no application person cap."""


def select_people(boxes, confidences, frame_height, max_people=0,
                  min_confidence=0.25, min_height_ratio=0.0):
    selected = [i for i, box in enumerate(boxes)
                if confidences[i] >= min_confidence
                and box[3] - box[1] >= min_height_ratio * frame_height]
    selected.sort(key=lambda i: (boxes[i][2] - boxes[i][0]) *
                  (boxes[i][3] - boxes[i][1]), reverse=True)
    return selected[:max_people] if max_people else selected
