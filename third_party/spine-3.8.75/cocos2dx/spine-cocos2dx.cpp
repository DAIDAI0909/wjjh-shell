/******************************************************************************
 * Spine Runtimes License Agreement
 * Last updated January 1, 2020. Replaces all prior versions.
 *
 * Copyright (c) 2013-2020, Esoteric Software LLC
 *
 * Integration of the Spine Runtimes into software or otherwise creating
 * derivative works of the Spine Runtimes is permitted under the terms and
 * conditions of Section 2 of the Spine Editor License Agreement:
 * http://esotericsoftware.com/spine-editor-license
 *
 * Otherwise, it is permitted to integrate the Spine Runtimes into software
 * or otherwise create derivative works of the Spine Runtimes (collectively,
 * "Products"), provided that each user of the Products must obtain their own
 * Spine Editor license and redistribution of the Products in any form must
 * include this license and copyright notice.
 *
 * THE SPINE RUNTIMES ARE PROVIDED BY ESOTERIC SOFTWARE LLC "AS IS" AND ANY
 * EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL ESOTERIC SOFTWARE LLC BE LIABLE FOR ANY
 * DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES,
 * BUSINESS INTERRUPTION, OR LOSS OF USE, DATA, OR PROFITS) HOWEVER CAUSED AND
 * ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
 * THE SPINE RUNTIMES, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *****************************************************************************/

#include <spine38/spine-cocos2dx.h>
#include <spine38/Extension.h>
#include <spine38/AttachmentVertices.h>

// gh242: 诊断输出用（meta-only 模式的跳过日志）。
#include <cstdio>

USING_NS_CC;
using namespace spine38;

static void deleteAttachmentVertices (void* vertices) {
	delete (AttachmentVertices *) vertices;
}

// ---- gh242: 元数据模式（跳过纹理加载）----------------------------------------
// 背景：游戏的 app/FightSystem/ResourceManager/AnimResManager.lua 在**模块加载时**
//   就调用 initAnimResInfo()，遍历 animRes 表（2138 条 / 4 个图集）为每项创建
//   SkeletonAnimation，只为读取动画时长(getAnimDuration)、事件(getAnimEvents)、
//   攻击距离(getBoneSetupPosePosition) —— **这些只需要 SkeletonData，不需要纹理**。
//   但 createWithBinaryFile → Atlas(atlas, &textureLoader, true) 会把图集**全部页**
//   载入并经 TextureCache 常驻。实测 gongfu1 一个图集就 1145 页 × 512² = 1145MB，
//   四个图集合计远超 2GB 设备约 1.4GB 的单进程上限 ⇒ 载入到 88% 被 Jetsam 杀。
//   （JetsamEvent-2026-10-15-132645 / 192834：name=WJJH reason=per-process-limit）
// 方案：打开本开关后 load() 直接返回，不创建 Texture2D；元数据 API 照常可用。
//   画质零影响 —— 真正需要渲染时（进战斗）会以正常模式重新创建。
static bool g_wjjhSkipTextureLoad = false;

namespace spine38 {
	bool wjjhSpine38SkipTextureLoad(bool skip) {
		bool old = g_wjjhSkipTextureLoad;
		g_wjjhSkipTextureLoad = skip;
		return old;
	}
	bool wjjhSpine38IsSkippingTextureLoad() {
		return g_wjjhSkipTextureLoad;
	}
}

static unsigned short quadTriangles[6] = {0, 1, 2, 2, 3, 0};

static void setAttachmentVertices(RegionAttachment* attachment) {
	AtlasRegion* region = (AtlasRegion*)attachment->getRendererObject();
	AttachmentVertices* attachmentVertices = new AttachmentVertices((Texture2D*)region->page->getRendererObject(), 4, quadTriangles, 6);
	V3F_C4B_T2F* vertices = attachmentVertices->_triangles->verts;
	for (int i = 0, ii = 0; i < 4; ++i, ii += 2) {
		vertices[i].texCoords.u = attachment->getUVs()[ii];
		vertices[i].texCoords.v = attachment->getUVs()[ii + 1];
	}
	attachment->setRendererObject(attachmentVertices, deleteAttachmentVertices);
}

static void setAttachmentVertices(MeshAttachment* attachment) {
	AtlasRegion* region = (AtlasRegion*)attachment->getRendererObject();
	AttachmentVertices* attachmentVertices = new AttachmentVertices((Texture2D*)region->page->getRendererObject(),
																	attachment->getWorldVerticesLength() >> 1, attachment->getTriangles().buffer(), attachment->getTriangles().size());
	V3F_C4B_T2F* vertices = attachmentVertices->_triangles->verts;
	for (int i = 0, ii = 0, nn = attachment->getWorldVerticesLength(); ii < nn; ++i, ii += 2) {
		vertices[i].texCoords.u = attachment->getUVs()[ii];
		vertices[i].texCoords.v = attachment->getUVs()[ii + 1];
	}
	attachment->setRendererObject(attachmentVertices, deleteAttachmentVertices);
}

Cocos2dAtlasAttachmentLoader::Cocos2dAtlasAttachmentLoader(Atlas* atlas): AtlasAttachmentLoader(atlas) {
}

Cocos2dAtlasAttachmentLoader::~Cocos2dAtlasAttachmentLoader() { }

void Cocos2dAtlasAttachmentLoader::configureAttachment(Attachment* attachment) {
	if (attachment->getRTTI().isExactly(RegionAttachment::rtti)) {
		setAttachmentVertices((RegionAttachment*)attachment);
	} else if (attachment->getRTTI().isExactly(MeshAttachment::rtti)) {
		setAttachmentVertices((MeshAttachment*)attachment);
	}
}

#if COCOS2D_VERSION >= 0x0040000

backend::SamplerAddressMode wrap (TextureWrap wrap) {
	return wrap ==  TextureWrap_ClampToEdge ? backend::SamplerAddressMode::CLAMP_TO_EDGE : backend::SamplerAddressMode::REPEAT;
}

backend::SamplerFilter filter (TextureFilter filter) {
	switch (filter) {
	case TextureFilter_Unknown:
		break;
	case TextureFilter_Nearest:
		return backend::SamplerFilter::NEAREST;
	case TextureFilter_Linear:
		return backend::SamplerFilter::LINEAR;
	case TextureFilter_MipMap:
		return backend::SamplerFilter::LINEAR;
	case TextureFilter_MipMapNearestNearest:
		return backend::SamplerFilter::NEAREST;
	case TextureFilter_MipMapLinearNearest:
        return backend::SamplerFilter::NEAREST;
	case TextureFilter_MipMapNearestLinear:
        return backend::SamplerFilter::LINEAR;
	case TextureFilter_MipMapLinearLinear:
        return backend::SamplerFilter::LINEAR;
	}
	return backend::SamplerFilter::LINEAR;
}

#else

GLuint wrap (TextureWrap wrap) {
	return wrap ==  TextureWrap_ClampToEdge ? GL_CLAMP_TO_EDGE : GL_REPEAT;
}

GLuint filter (TextureFilter filter) {
	switch (filter) {
	case TextureFilter_Unknown:
		break;
	case TextureFilter_Nearest:
		return GL_NEAREST;
	case TextureFilter_Linear:
		return GL_LINEAR;
	case TextureFilter_MipMap:
		return GL_LINEAR_MIPMAP_LINEAR;
	case TextureFilter_MipMapNearestNearest:
		return GL_NEAREST_MIPMAP_NEAREST;
	case TextureFilter_MipMapLinearNearest:
		return GL_LINEAR_MIPMAP_NEAREST;
	case TextureFilter_MipMapNearestLinear:
		return GL_NEAREST_MIPMAP_LINEAR;
	case TextureFilter_MipMapLinearLinear:
		return GL_LINEAR_MIPMAP_LINEAR;
	}
	return GL_LINEAR;
}

#endif

Cocos2dTextureLoader::Cocos2dTextureLoader() : TextureLoader() { }
Cocos2dTextureLoader::~Cocos2dTextureLoader() { }

void Cocos2dTextureLoader::load(AtlasPage& page, const spine38::String& path) {
	// gh242: 元数据模式 —— 不建纹理，只让 Atlas 记住页码尺寸（spine 元数据仍完整）。
	//   rendererObject 保持 NULL：本模式下产生的 SkeletonAnimation 只用于读
	//   duration/events/bone-position，绝不参与渲染（渲染路径会另行正常创建）。
	if (g_wjjhSkipTextureLoad) {
		static int s_skipLogged = 0;
		if (s_skipLogged < 6) {
			fprintf(stderr, "WJJH_SPINE38: [meta-only] skip texture load: %s\n", path.buffer());
			++s_skipLogged;
		}
		return;
	}
	// gh269: 强制 RGBA4444。Texture2D::setDefaultAlphaPixelFormat(RGBA4444)（AppDelegate
	//   低内存设备分支）实测对 addImage 未生效（缓存探针 @32bpp）⇒ gongfu 1620 页
	//   8888=405MB、特效合计 120MB ⇒ 战斗期 1.42GB 被 Jetsam 杀
	//   （JetsamEvent-2026-10-18-001417, per-process-limit 1450MB）。4444 减半 ≈264MB。
	cocos2d::Texture2D* texture = nullptr;
	{
		cocos2d::Image img;
		if (img.initWithImageFile(path.buffer())) {
			texture = new (std::nothrow) cocos2d::Texture2D();
			if (texture != nullptr) {
				texture->autorelease();  // gh269: 池排水时抵消 new 的引用，页持有靠下方 retain
				if (!texture->initWithImage(&img, cocos2d::Texture2D::PixelFormat::RGBA4444)) {
					texture->release();
					texture = nullptr;
				}
			}
		}
	}
	if (texture == nullptr) {
		// 兜底：标准 addImage（原路径）
		texture = Director::getInstance()->getTextureCache()->addImage(path.buffer());
	}
	CCASSERT(texture != nullptr, "Invalid image");
	if (texture) {
		texture->retain();
#if COCOS2D_VERSION >= 0x0040000
		Texture2D::TexParams textureParams(filter(page.minFilter), filter(page.magFilter), wrap(page.uWrap), wrap(page.vWrap));
#else
		Texture2D::TexParams textureParams = {filter(page.minFilter), filter(page.magFilter), wrap(page.uWrap), wrap(page.vWrap)};
#endif
		texture->setTexParameters(textureParams);

		page.setRendererObject(texture);
		page.width = texture->getPixelsWide();
		page.height = texture->getPixelsHigh();
	}
}

void Cocos2dTextureLoader::unload(void* texture) {
	if (texture) {
		((Texture2D*)texture)->release();
	}
}


Cocos2dExtension::Cocos2dExtension() : DefaultSpineExtension() { }

Cocos2dExtension::~Cocos2dExtension() { }

char *Cocos2dExtension::_readFile(const spine38::String &path, int *length) {
    Data data = FileUtils::getInstance()->getDataFromFile(path.buffer());
	if (data.isNull()) return nullptr;

	// avoid buffer overflow (int is shorter than ssize_t in certain platforms)
#if COCOS2D_VERSION >= 0x00031200
	ssize_t tmpLen;
	char *ret = (char*)data.takeBuffer(&tmpLen);
	*length = static_cast<int>(tmpLen);
	return ret;
#else
	*length = static_cast<int>(data.getSize());
    auto bytes = SpineExtension::alloc<char>(*length, __FILE__, __LINE__);
	memcpy(bytes, data.getBytes(), *length);
	return bytes;
#endif
}

SpineExtension *spine38::getDefaultExtension () {
	return new Cocos2dExtension();
}
