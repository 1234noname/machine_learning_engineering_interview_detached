"""ViT-b-16 image classification served via FastAPI.

Lazy model instantiation — `vit_b_16(pretrained=True)` downloads ~330MB
of weights and is slow + network-dependent. Tests mock `get_model()` to
avoid the load entirely; production / first real `/predict` call triggers
the load on demand.
"""

from io import BytesIO

import requests
import torch
from fastapi import FastAPI
from PIL import Image
from prometheus_fastapi_instrumentator import Instrumentator
from torchvision import transforms
from torchvision.models import vit_b_16


class ImageModel:
    def __init__(self) -> None:
        self.model = vit_b_16(pretrained=True).eval()
        self.preprocessor = transforms.Compose(
            [
                transforms.Resize(224),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Lambda(lambda t: t[:3, ...]),  # remove alpha channel
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
                ),
            ]
        )

    def predict(self, image_url: str) -> dict[str, int]:
        response = requests.get(image_url, timeout=30)
        pil_image = Image.open(BytesIO(response.content))

        pil_images = [pil_image]  # Batch size of 1
        input_tensor = torch.cat(
            [self.preprocessor(i).unsqueeze(0) for i in pil_images]
        )

        with torch.no_grad():
            output_tensor = self.model(input_tensor)
        return {"class_index": int(torch.argmax(output_tensor[0]))}


app = FastAPI()
Instrumentator().instrument(app).expose(app)
_model_instance: ImageModel | None = None


def get_model() -> ImageModel:
    """Lazy ImageModel singleton. Triggers ViT weight download on first call.

    Tests monkey-patch this function to return a mock so the real load
    never happens during pytest runs.
    """
    global _model_instance
    if _model_instance is None:
        _model_instance = ImageModel()
    return _model_instance


@app.get("/predict")
async def predict(image_url: str) -> dict[str, int]:
    return get_model().predict(image_url)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
