from datetime import datetime
from typing import Any, Literal, Optional

import filetype
from fastapi import Depends, FastAPI, Form, HTTPException, Response, UploadFile
from pil_utils.types import ColorType, FontStyle, FontWeight
from pydantic import BaseModel, ValidationError

from meme_generator.config import meme_config
from meme_generator.exception import MemeGeneratorException, NoSuchMeme
from meme_generator.log import LOGGING_CONFIG, setup_logger
from meme_generator.manager import get_meme, get_meme_keys, get_memes
from meme_generator.meme import CommandShortcut, Meme, MemeArgsModel
from meme_generator.utils import TextProperties, render_meme_list, run_sync

app = FastAPI()


class MemeArgsResponse(BaseModel):
    name: str
    type: str
    description: Optional[str] = None
    default: Optional[Any] = None
    enum: Optional[list[Any]] = None


class MemeParamsResponse(BaseModel):
    min_images: int
    max_images: int
    min_texts: int
    max_texts: int
    default_texts: list[str]
    args: list[MemeArgsResponse]


class MemeInfoResponse(BaseModel):
    key: str
    keywords: list[str]
    shortcuts: list[CommandShortcut]
    tags: list[str]
    params: MemeParamsResponse
    date_created: datetime
    date_modified: datetime


def register_router(meme: Meme):
    if args_type := meme.params_type.args_type:
        args_model = args_type.args_model
    else:
        args_model = MemeArgsModel

    def args_checker(args: Optional[str] = Form(default=str(args_model().json()))):
        if not args:
            return MemeArgsModel()
        try:
            model = args_model.parse_raw(args)
        except ValidationError as e:
            raise HTTPException(status_code=552, detail=str(e))
        return model

    @app.post(f"/memes/{meme.key}/")
    async def _(
        images: list[UploadFile] = [],
        texts: list[str] = meme.params_type.default_texts,
        args: args_model = Depends(args_checker),  # type: ignore
    ):
        imgs: list[bytes] = []
        for image in images:
            imgs.append(await image.read())

        texts = [text for text in texts if text]

        assert isinstance(args, args_model)

        try:
            result = await run_sync(meme)(images=imgs, texts=texts, args=args.dict())
        except MemeGeneratorException as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))

        content = result.getvalue()
        media_type = str(filetype.guess_mime(content)) or "text/plain"
        return Response(content=content, media_type=media_type)


class MemeKeyWithProperties(BaseModel):
    meme_key: str
    fill: ColorType = "black"
    style: FontStyle = "normal"
    weight: FontWeight = "normal"
    stroke_width: int = 0
    stroke_fill: Optional[ColorType] = None


default_meme_list = [
    MemeKeyWithProperties(meme_key=meme.key)
    for meme in sorted(get_memes(), key=lambda meme: meme.key)
]


class RenderMemeListRequest(BaseModel):
    meme_list: list[MemeKeyWithProperties] = default_meme_list
    order_direction: Literal["row", "column"] = "column"
    columns: int = 4
    column_align: Literal["left", "center", "right"] = "left"
    item_padding: tuple[int, int] = (15, 2)
    image_padding: tuple[int, int] = (50, 50)
    bg_color: ColorType = "white"
    fontsize: int = 30
    fontname: str = ""
    fallback_fonts: list[str] = []


def register_routers():
    @app.post("/memes/render_list")
    def _(params: RenderMemeListRequest = RenderMemeListRequest()):
        try:
            meme_list = [
                (
                    get_meme(p.meme_key),
                    TextProperties(
                        fill=p.fill,
                        style=p.style,
                        weight=p.weight,
                        stroke_width=p.stroke_width,
                        stroke_fill=p.stroke_fill,
                    ),
                )
                for p in params.meme_list
            ]
        except NoSuchMeme as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))

        result = render_meme_list(
            meme_list,
            order_direction=params.order_direction,
            columns=params.columns,
            column_align=params.column_align,
            item_padding=params.item_padding,
            image_padding=params.image_padding,
            bg_color=params.bg_color,
            fontsize=params.fontsize,
            fontname=params.fontname,
            fallback_fonts=params.fallback_fonts,
        )
        content = result.getvalue()
        media_type = str(filetype.guess_mime(content)) or "text/plain"
        return Response(content=content, media_type=media_type)

    @app.get("/memes/keys")
    def _():
        return get_meme_keys()

    @app.get("/memes/{key}/info")
    def _(key: str):
        try:
            meme = get_meme(key)
        except NoSuchMeme as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))

        args_model = (
            meme.params_type.args_type.args_model
            if meme.params_type.args_type
            else MemeArgsModel
        )
        properties: dict[str, dict[str, Any]] = (
            args_model.schema().get("properties", {}).copy()
        )
        properties.pop("user_infos")
        return MemeInfoResponse(
            key=meme.key,
            keywords=meme.keywords,
            shortcuts=meme.shortcuts,
            tags=meme.tags,
            params=MemeParamsResponse(
                min_images=meme.params_type.min_images,
                max_images=meme.params_type.max_images,
                min_texts=meme.params_type.min_texts,
                max_texts=meme.params_type.max_texts,
                default_texts=meme.params_type.default_texts,
                args=[
                    MemeArgsResponse(
                        name=name,
                        type=info.get("type", ""),
                        description=info.get("description"),
                        default=info.get("default"),
                        enum=info.get("enum"),
                    )
                    for name, info in properties.items()
                ],
            ),
            date_created=meme.date_created,
            date_modified=meme.date_modified,
        )

    @app.get("/memes/{key}/preview")
    async def _(key: str):
        try:
            meme = get_meme(key)
            result = await run_sync(meme.generate_preview)()
        except MemeGeneratorException as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))

        content = result.getvalue()
        media_type = str(filetype.guess_mime(content)) or "text/plain"
        return Response(content=content, media_type=media_type)

    for meme in sorted(get_memes(), key=lambda meme: meme.key):
        register_router(meme)


def run_server():
    import uvicorn

    register_routers()
    uvicorn.run(
        app,
        host=meme_config.server.host,
        port=meme_config.server.port,
        log_config=LOGGING_CONFIG,
    )


if __name__ == "__main__":
    setup_logger()
    run_server()
