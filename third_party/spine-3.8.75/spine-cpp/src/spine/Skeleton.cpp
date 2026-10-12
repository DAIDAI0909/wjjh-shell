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

#ifdef SPINE_UE4
#include "SpinePluginPrivatePCH.h"
#endif

#include <spine38/Skeleton.h>

#include <spine38/SkeletonData.h>
#include <spine38/Bone.h>
#include <spine38/Slot.h>
#include <spine38/IkConstraint.h>
#include <spine38/PathConstraint.h>
#include <spine38/TransformConstraint.h>
#include <spine38/Skin.h>
#include <spine38/Attachment.h>

#include <spine38/BoneData.h>
#include <spine38/SlotData.h>
#include <spine38/IkConstraintData.h>
#include <spine38/TransformConstraintData.h>
#include <spine38/PathConstraintData.h>
#include <spine38/RegionAttachment.h>
#include <spine38/MeshAttachment.h>
#include <spine38/PathAttachment.h>

#include <spine38/ContainerUtil.h>

#include <float.h>

using namespace spine38;

Skeleton::Skeleton(SkeletonData *skeletonData) :
		_data(skeletonData),
		_skin(NULL),
		_color(1, 1, 1, 1),
		_time(0),
		_scaleX(1),
		_scaleY(1),
		_x(0),
		_y(0) {
	// gh221: SkeletonBinary::readSkeletonData 读取失败时返回 NULL（例如附件在 atlas 里找不到
	//   → newRegionAttachment 返回 NULL → setError → readSkeletonData 返回 NULL）。
	//   而 SkeletonRenderer::initWithBinaryFile 里的 CCASSERT 在 Release 下是 no-op，
	//   NULL 会被原样交给本构造函数 → 这里解引用 _data 立刻 SIGSEGV（run 220 实测）。
	//   现在遇到 NULL 就构造一个"空骨架"：容器保持为空，getData() 仍然返回 NULL，
	//   调用方（C 桥 wjjh_spine38_createBinary）据此判定失败并回退哑节点。
	if (_data == NULL) {
		fprintf(stderr, "WJJH_SPINE38: Skeleton ctor got NULL SkeletonData -> empty skeleton\n");
		return;
	}

	// gh221 取证：ctor 逐段打点（stderr 在 main.mm 里设成行缓冲，崩溃前一定能落盘）。
	//   只打点前 3 个骨架，避免日志被刷爆。
	static int s_ctorTrace = 0;
	const bool ctorTrace = s_ctorTrace < 3;
	if (ctorTrace) s_ctorTrace++;
	if (ctorTrace) fprintf(stderr, "WJJH_SPINE38 CTOR enter data=%p bones=%d slots=%d ik=%d tc=%d pc=%d anims=%d defaultSkin=%d\n",
		(void *)_data, (int)_data->getBones().size(), (int)_data->getSlots().size(),
		(int)_data->getIkConstraints().size(), (int)_data->getTransformConstraints().size(),
		(int)_data->getPathConstraints().size(), (int)_data->getAnimations().size(),
		_data->getDefaultSkin() != NULL ? 1 : 0);

	_bones.ensureCapacity(_data->getBones().size());
	for (size_t i = 0; i < _data->getBones().size(); ++i) {
		BoneData *data = _data->getBones()[i];
		if (data == NULL) break; // 损坏/截断数据：无法建骨骼，停止（调用方另有兜底）

		Bone *bone;
		// gh221: 父骨索引越界（损坏/截断数据）时当作根骨骼处理，避免 _bones[野索引] 崩溃。
		if (data->getParent() == NULL || (size_t)data->getParent()->getIndex() >= _bones.size()) {
			bone = new(__FILE__, __LINE__) Bone(*data, *this, NULL);
		} else {
			Bone *parent = _bones[data->getParent()->getIndex()];
			bone = new(__FILE__, __LINE__) Bone(*data, *this, parent);
			parent->getChildren().add(bone);
		}

		_bones.add(bone);
	}
	fprintf(stderr, "WJJH_SPINE38 CTOR bones-done n=%d\n", (int)_bones.size());

	_slots.ensureCapacity(_data->getSlots().size());
	_drawOrder.ensureCapacity(_data->getSlots().size());
	for (size_t i = 0; i < _data->getSlots().size(); ++i) {
		SlotData *data = _data->getSlots()[i];
		if (data == NULL || _bones.size() == 0) break;

		// gh221: 插槽的骨骼索引越界时钳到最后一根骨骼，同样避免野指针解引用。
		size_t boneIndex = (size_t)data->getBoneData().getIndex();
		if (boneIndex >= _bones.size()) boneIndex = _bones.size() - 1;
		Bone *bone = _bones[boneIndex];
		if (ctorTrace) {
			const char *attName = data->getAttachmentName().buffer();
			fprintf(stderr, "WJJH_SPINE38 CTOR slot[%d] begin bone=%d att=%s\n",
				(int)i, (int)boneIndex, attName ? attName : "");
		}
		Slot *slot = new(__FILE__, __LINE__) Slot(*data, *bone);

		_slots.add(slot);
		_drawOrder.add(slot);
	}
	fprintf(stderr, "WJJH_SPINE38 CTOR slots-done n=%d\n", (int)_slots.size());

	_ikConstraints.ensureCapacity(_data->getIkConstraints().size());
	for (size_t i = 0; i < _data->getIkConstraints().size(); ++i) {
		IkConstraintData *data = _data->getIkConstraints()[i];

		IkConstraint *constraint = new(__FILE__, __LINE__) IkConstraint(*data, *this);

		_ikConstraints.add(constraint);
	}

	_transformConstraints.ensureCapacity(_data->getTransformConstraints().size());
	for (size_t i = 0; i < _data->getTransformConstraints().size(); ++i) {
		TransformConstraintData *data = _data->getTransformConstraints()[i];

		TransformConstraint *constraint = new(__FILE__, __LINE__) TransformConstraint(*data, *this);

		_transformConstraints.add(constraint);
	}

	_pathConstraints.ensureCapacity(_data->getPathConstraints().size());
	for (size_t i = 0; i < _data->getPathConstraints().size(); ++i) {
		PathConstraintData *data = _data->getPathConstraints()[i];

		PathConstraint *constraint = new(__FILE__, __LINE__) PathConstraint(*data, *this);

		_pathConstraints.add(constraint);
	}

	fprintf(stderr, "WJJH_SPINE38 CTOR constraints-done ik=%d tc=%d pc=%d\n",
		(int)_ikConstraints.size(), (int)_transformConstraints.size(), (int)_pathConstraints.size());

	updateCache();
	fprintf(stderr, "WJJH_SPINE38 CTOR updateCache-done -> ctor OK\n");
}

Skeleton::~Skeleton() {
	ContainerUtil::cleanUpVectorOfPointers(_bones);
	ContainerUtil::cleanUpVectorOfPointers(_slots);
	ContainerUtil::cleanUpVectorOfPointers(_ikConstraints);
	ContainerUtil::cleanUpVectorOfPointers(_transformConstraints);
	ContainerUtil::cleanUpVectorOfPointers(_pathConstraints);
}

void Skeleton::updateCache() {
	_updateCache.clear();
	_updateCacheReset.clear();

	for (size_t i = 0, n = _bones.size(); i < n; ++i) {
		Bone* bone = _bones[i];
		bone->_sorted = bone->_data.isSkinRequired();
		bone->_active = !bone->_sorted;
	}

	if (_skin) {
		Vector<BoneData*>& skinBones = _skin->getBones();
		for (size_t i = 0, n = skinBones.size(); i < n; i++) {
			Bone* bone = _bones[skinBones[i]->getIndex()];
			do {
				bone->_sorted = false;
				bone->_active = true;
				bone = bone->_parent;
			} while (bone);
		}
	}

	size_t ikCount = _ikConstraints.size();
	size_t transformCount = _transformConstraints.size();
	size_t pathCount = _pathConstraints.size();

	size_t constraintCount = ikCount + transformCount + pathCount;

	size_t i = 0;
	continue_outer:
	for (; i < constraintCount; ++i) {
		for (size_t ii = 0; ii < ikCount; ++ii) {
			IkConstraint *constraint = _ikConstraints[ii];
			if (constraint->getData().getOrder() == i) {
				sortIkConstraint(constraint);
				i++;
				goto continue_outer;
			}
		}

		for (size_t ii = 0; ii < transformCount; ++ii) {
			TransformConstraint *constraint = _transformConstraints[ii];
			if (constraint->getData().getOrder() == i) {
				sortTransformConstraint(constraint);
				i++;
				goto continue_outer;
			}
		}

		for (size_t ii = 0; ii < pathCount; ++ii) {
			PathConstraint *constraint = _pathConstraints[ii];
			if (constraint->getData().getOrder() == i) {
				sortPathConstraint(constraint);
				i++;
				goto continue_outer;
			}
		}
	}

	size_t n = _bones.size();
	for (i = 0; i < n; ++i) {
		sortBone(_bones[i]);
	}
}

void Skeleton::printUpdateCache() {
	for (size_t i = 0; i < _updateCache.size(); i++) {
		Updatable *updatable = _updateCache[i];
		if (updatable->getRTTI().isExactly(Bone::rtti)) {
			printf("bone %s\n", ((Bone *) updatable)->getData().getName().buffer());
		} else if (updatable->getRTTI().isExactly(TransformConstraint::rtti)) {
			printf("transform constraint %s\n", ((TransformConstraint *) updatable)->getData().getName().buffer());
		} else if (updatable->getRTTI().isExactly(IkConstraint::rtti)) {
			printf("ik constraint %s\n", ((IkConstraint *) updatable)->getData().getName().buffer());
		} else if (updatable->getRTTI().isExactly(PathConstraint::rtti)) {
			printf("path constraint %s\n", ((PathConstraint *) updatable)->getData().getName().buffer());
		}
	}
}

void Skeleton::updateWorldTransform() {
	for (size_t i = 0, n = _updateCacheReset.size(); i < n; ++i) {
		Bone *boneP = _updateCacheReset[i];
		Bone &bone = *boneP;
		bone._ax = bone._x;
		bone._ay = bone._y;
		bone._arotation = bone._rotation;
		bone._ascaleX = bone._scaleX;
		bone._ascaleY = bone._scaleY;
		bone._ashearX = bone._shearX;
		bone._ashearY = bone._shearY;
		bone._appliedValid = true;
	}

	for (size_t i = 0, n = _updateCache.size(); i < n; ++i) {
		_updateCache[i]->update();
	}
}

void Skeleton::setToSetupPose() {
	setBonesToSetupPose();
	setSlotsToSetupPose();
}

void Skeleton::setBonesToSetupPose() {
	for (size_t i = 0, n = _bones.size(); i < n; ++i) {
		_bones[i]->setToSetupPose();
	}

	for (size_t i = 0, n = _ikConstraints.size(); i < n; ++i) {
		IkConstraint *constraintP = _ikConstraints[i];
		IkConstraint &constraint = *constraintP;

		constraint._bendDirection = constraint._data._bendDirection;
		constraint._compress = constraint._data._compress;
		constraint._stretch = constraint._data._stretch;
		constraint._mix = constraint._data._mix;
		constraint._softness = constraint._data._softness;
	}

	for (size_t i = 0, n = _transformConstraints.size(); i < n; ++i) {
		TransformConstraint *constraintP = _transformConstraints[i];
		TransformConstraint &constraint = *constraintP;
		TransformConstraintData &constraintData = constraint._data;

		constraint._rotateMix = constraintData._rotateMix;
		constraint._translateMix = constraintData._translateMix;
		constraint._scaleMix = constraintData._scaleMix;
		constraint._shearMix = constraintData._shearMix;
	}

	for (size_t i = 0, n = _pathConstraints.size(); i < n; ++i) {
		PathConstraint *constraintP = _pathConstraints[i];
		PathConstraint &constraint = *constraintP;
		PathConstraintData &constraintData = constraint._data;

		constraint._position = constraintData._position;
		constraint._spacing = constraintData._spacing;
		constraint._rotateMix = constraintData._rotateMix;
		constraint._translateMix = constraintData._translateMix;
	}
}

void Skeleton::setSlotsToSetupPose() {
	_drawOrder.clear();
	for (size_t i = 0, n = _slots.size(); i < n; ++i) {
		_drawOrder.add(_slots[i]);
	}

	for (size_t i = 0, n = _slots.size(); i < n; ++i) {
		_slots[i]->setToSetupPose();
	}
}

Bone *Skeleton::findBone(const String &boneName) {
	return ContainerUtil::findWithDataName(_bones, boneName);
}

int Skeleton::findBoneIndex(const String &boneName) {
	return ContainerUtil::findIndexWithDataName(_bones, boneName);
}

Slot *Skeleton::findSlot(const String &slotName) {
	return ContainerUtil::findWithDataName(_slots, slotName);
}

int Skeleton::findSlotIndex(const String &slotName) {
	return ContainerUtil::findIndexWithDataName(_slots, slotName);
}

void Skeleton::setSkin(const String &skinName) {
	Skin *foundSkin = skinName.isEmpty() ? NULL : _data->findSkin(skinName);
	setSkin(foundSkin);
}

void Skeleton::setSkin(Skin *newSkin) {
	if (_skin == newSkin) return;
	if (newSkin != NULL) {
		if (_skin != NULL) {
			Skeleton &thisRef = *this;
			newSkin->attachAll(thisRef, *_skin);
		} else {
			for (size_t i = 0, n = _slots.size(); i < n; ++i) {
				Slot *slotP = _slots[i];
				Slot &slot = *slotP;
				const String &name = slot._data.getAttachmentName();
				if (name.length() > 0) {
					Attachment *attachment = newSkin->getAttachment(i, name);
					if (attachment != NULL) {
						slot.setAttachment(attachment);
					}
				}
			}
		}
	}

	_skin = newSkin;
	updateCache();
}

Attachment *Skeleton::getAttachment(const String &slotName, const String &attachmentName) {
	return getAttachment(_data->findSlotIndex(slotName), attachmentName);
}

Attachment *Skeleton::getAttachment(int slotIndex, const String &attachmentName) {
	if (attachmentName.isEmpty()) return NULL;

	if (_skin != NULL) {
		Attachment *attachment = _skin->getAttachment(slotIndex, attachmentName);
		if (attachment != NULL) {
			return attachment;
		}
	}

	return _data->getDefaultSkin() != NULL ? _data->getDefaultSkin()->getAttachment(slotIndex, attachmentName) : NULL;
}

void Skeleton::setAttachment(const String &slotName, const String &attachmentName) {
	assert(slotName.length() > 0);

	for (size_t i = 0, n = _slots.size(); i < n; ++i) {
		Slot *slot = _slots[i];
		if (slot->_data.getName() == slotName) {
			Attachment *attachment = NULL;
			if (attachmentName.length() > 0) {
				attachment = getAttachment(i, attachmentName);

				assert(attachment != NULL);
			}

			slot->setAttachment(attachment);

			return;
		}
	}

	printf("Slot not found: %s", slotName.buffer());

	assert(false);
}

IkConstraint *Skeleton::findIkConstraint(const String &constraintName) {
	assert(constraintName.length() > 0);

	for (size_t i = 0, n = _ikConstraints.size(); i < n; ++i) {
		IkConstraint *ikConstraint = _ikConstraints[i];
		if (ikConstraint->_data.getName() == constraintName) {
			return ikConstraint;
		}
	}
	return NULL;
}

TransformConstraint *Skeleton::findTransformConstraint(const String &constraintName) {
	assert(constraintName.length() > 0);

	for (size_t i = 0, n = _transformConstraints.size(); i < n; ++i) {
		TransformConstraint *transformConstraint = _transformConstraints[i];
		if (transformConstraint->_data.getName() == constraintName) {
			return transformConstraint;
		}
	}

	return NULL;
}

PathConstraint *Skeleton::findPathConstraint(const String &constraintName) {
	assert(constraintName.length() > 0);

	for (size_t i = 0, n = _pathConstraints.size(); i < n; ++i) {
		PathConstraint *constraint = _pathConstraints[i];
		if (constraint->_data.getName() == constraintName) {
			return constraint;
		}
	}

	return NULL;
}

void Skeleton::update(float delta) {
	_time += delta;
}

void Skeleton::getBounds(float &outX, float &outY, float &outWidth, float &outHeight, Vector<float> &outVertexBuffer) {
	float minX = FLT_MAX;
	float minY = FLT_MAX;
	float maxX = FLT_MIN;
	float maxY = FLT_MIN;

	for (size_t i = 0; i < _drawOrder.size(); ++i) {
		Slot *slot = _drawOrder[i];
		if (!slot->_bone._active) continue;
		size_t verticesLength = 0;
		Attachment *attachment = slot->getAttachment();

		if (attachment != NULL && attachment->getRTTI().instanceOf(RegionAttachment::rtti)) {
			RegionAttachment *regionAttachment = static_cast<RegionAttachment *>(attachment);

			verticesLength = 8;
			if (outVertexBuffer.size() < 8) {
				outVertexBuffer.setSize(8, 0);
			}
			regionAttachment->computeWorldVertices(slot->getBone(), outVertexBuffer, 0);
		} else if (attachment != NULL && attachment->getRTTI().instanceOf(MeshAttachment::rtti)) {
			MeshAttachment *mesh = static_cast<MeshAttachment *>(attachment);

			verticesLength = mesh->getWorldVerticesLength();
			if (outVertexBuffer.size() < verticesLength) {
				outVertexBuffer.setSize(verticesLength, 0);
			}

			mesh->computeWorldVertices(*slot, 0, verticesLength, outVertexBuffer, 0);
		}

		for (size_t ii = 0; ii < verticesLength; ii += 2) {
			float vx = outVertexBuffer[ii];
			float vy = outVertexBuffer[ii + 1];

			minX = MathUtil::min(minX, vx);
			minY = MathUtil::min(minY, vy);
			maxX = MathUtil::max(maxX, vx);
			maxY = MathUtil::max(maxY, vy);
		}
	}

	outX = minX;
	outY = minY;
	outWidth = maxX - minX;
	outHeight = maxY - minY;
}

Bone *Skeleton::getRootBone() {
	return _bones.size() == 0 ? NULL : _bones[0];
}

SkeletonData *Skeleton::getData() {
	return _data;
}

Vector<Bone *> &Skeleton::getBones() {
	return _bones;
}

Vector<Updatable *> &Skeleton::getUpdateCacheList() {
	return _updateCache;
}

Vector<Slot *> &Skeleton::getSlots() {
	return _slots;
}

Vector<Slot *> &Skeleton::getDrawOrder() {
	return _drawOrder;
}

Vector<IkConstraint *> &Skeleton::getIkConstraints() {
	return _ikConstraints;
}

Vector<PathConstraint *> &Skeleton::getPathConstraints() {
	return _pathConstraints;
}

Vector<TransformConstraint *> &Skeleton::getTransformConstraints() {
	return _transformConstraints;
}

Skin *Skeleton::getSkin() {
	return _skin;
}

Color &Skeleton::getColor() {
	return _color;
}

float Skeleton::getTime() {
	return _time;
}

void Skeleton::setTime(float inValue) {
	_time = inValue;
}

void Skeleton::setPosition(float x, float y) {
	_x = x;
	_y = y;
}

float Skeleton::getX() {
	return _x;
}

void Skeleton::setX(float inValue) {
	_x = inValue;
}

float Skeleton::getY() {
	return _y;
}

void Skeleton::setY(float inValue) {
	_y = inValue;
}

float Skeleton::getScaleX() {
	return _scaleX;
}

void Skeleton::setScaleX(float inValue) {
	_scaleX = inValue;
}

float Skeleton::getScaleY() {
	return _scaleY * (Bone::isYDown() ? -1 : 1);
}

void Skeleton::setScaleY(float inValue) {
	_scaleY = inValue;
}

void Skeleton::sortIkConstraint(IkConstraint *constraint) {
	constraint->_active = constraint->_target->_active && (!constraint->_data.isSkinRequired() || (_skin && _skin->_constraints.contains(&constraint->_data)));
	if (!constraint->_active) return;

	Bone *target = constraint->getTarget();
	sortBone(target);

	Vector<Bone *> &constrained = constraint->getBones();
	Bone *parent = constrained[0];
	sortBone(parent);

	if (constrained.size() > 1) {
		Bone *child = constrained[constrained.size() - 1];
		if (!_updateCache.contains(child)) _updateCacheReset.add(child);
	}

	_updateCache.add(constraint);

	sortReset(parent->getChildren());
	constrained[constrained.size() - 1]->_sorted = true;
}

void Skeleton::sortPathConstraint(PathConstraint *constraint) {
	constraint->_active = constraint->_target->_bone._active && (!constraint->_data.isSkinRequired() || (_skin && _skin->_constraints.contains(&constraint->_data)));
	if (!constraint->_active) return;

	Slot *slot = constraint->getTarget();
	int slotIndex = slot->getData().getIndex();
	Bone &slotBone = slot->getBone();
	if (_skin != NULL) sortPathConstraintAttachment(_skin, slotIndex, slotBone);
	if (_data->_defaultSkin != NULL && _data->_defaultSkin != _skin)
		sortPathConstraintAttachment(_data->_defaultSkin, slotIndex, slotBone);
	for (size_t ii = 0, nn = _data->_skins.size(); ii < nn; ii++)
		sortPathConstraintAttachment(_data->_skins[ii], slotIndex, slotBone);

	Attachment *attachment = slot->getAttachment();
	if (attachment != NULL && attachment->getRTTI().instanceOf(PathAttachment::rtti))
		sortPathConstraintAttachment(attachment, slotBone);

	Vector<Bone *> &constrained = constraint->getBones();
	size_t boneCount = constrained.size();
	for (size_t i = 0; i < boneCount; ++i) {
		sortBone(constrained[i]);
	}

	_updateCache.add(constraint);

	for (size_t i = 0; i < boneCount; i++)
		sortReset(constrained[i]->getChildren());
	for (size_t i = 0; i < boneCount; i++)
		constrained[i]->_sorted = true;
}

void Skeleton::sortTransformConstraint(TransformConstraint *constraint) {
	constraint->_active = constraint->_target->_active && (!constraint->_data.isSkinRequired() || (_skin && _skin->_constraints.contains(&constraint->_data)));
	if (!constraint->_active) return;

	sortBone(constraint->getTarget());

	Vector<Bone *> &constrained = constraint->getBones();
	size_t boneCount = constrained.size();
	if (constraint->_data.isLocal()) {
		for (size_t i = 0; i < boneCount; i++) {
			Bone *child = constrained[i];
			sortBone(child->getParent());
			if (!_updateCache.contains(child)) _updateCacheReset.add(child);
		}
	} else {
		for (size_t i = 0; i < boneCount; ++i) {
			sortBone(constrained[i]);
		}
	}

	_updateCache.add(constraint);

	for (size_t i = 0; i < boneCount; ++i)
		sortReset(constrained[i]->getChildren());
	for (size_t i = 0; i < boneCount; ++i)
		constrained[i]->_sorted = true;
}

void Skeleton::sortPathConstraintAttachment(Skin *skin, size_t slotIndex, Bone &slotBone) {
	Skin::AttachmentMap::Entries attachments = skin->getAttachments();

	while (attachments.hasNext()) {
		Skin::AttachmentMap::Entry entry = attachments.next();
		if (entry._slotIndex == slotIndex) {
			Attachment *value = entry._attachment;
			sortPathConstraintAttachment(value, slotBone);
		}
	}
}

void Skeleton::sortPathConstraintAttachment(Attachment *attachment, Bone &slotBone) {
	if (attachment == NULL || !attachment->getRTTI().instanceOf(PathAttachment::rtti)) return;
	Vector<size_t> &pathBones = static_cast<PathAttachment *>(attachment)->getBones();
	if (pathBones.size() == 0)
		sortBone(&slotBone);
	else {
		for (size_t i = 0, n = pathBones.size(); i < n;) {
			size_t nn = pathBones[i++];
			nn += i;
			while (i < nn) {
				sortBone(_bones[pathBones[i++]]);
			}
		}
	}
}

void Skeleton::sortBone(Bone *bone) {
	if (bone->_sorted) return;
	Bone *parent = bone->_parent;
	if (parent != NULL) sortBone(parent);
	bone->_sorted = true;
	_updateCache.add(bone);
}

void Skeleton::sortReset(Vector<Bone *> &bones) {
	for (size_t i = 0, n = bones.size(); i < n; ++i) {
		Bone *bone = bones[i];
		if (!bone->_active) continue;
		if (bone->_sorted) sortReset(bone->getChildren());
		bone->_sorted = false;
	}
}
