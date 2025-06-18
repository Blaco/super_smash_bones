bl_info = {
    "name": "Super Smash Bones",
    "author": "Taco",
    "version": (1, 0),
    "blender": (2, 79, 0),
    "location": "View3D > Tools",
    "description": "Various tools for converting character bones and animations between Smash 4 and Ultimate.",
    "doc_url": "https://github.com/Blaco/Super-Smash-Bones",
    "category": "Rigging"
}

import bpy
import bpy.utils.previews
import os
import re
import math
from mathutils import Quaternion
from .bonemaps import CHARACTER_BONE_MAPS
from mathutils import Matrix, Vector

# Global variables
custom_icons = None
NULL_RE  = re.compile(r"^S_(\D+?)(\d+)_null$")
SWING_RE = re.compile(r"^S_(\D+?)(\d+)$")

# ------------------------------------------------------------
# Scene-level Properties
# ------------------------------------------------------------
def character_items_scene(self, context):
    # "Common" should always appear at the top.
    items = [("Common", "Common", "Use common bone mapping")]
    for k in CHARACTER_BONE_MAPS.keys():
        if k != "Common":
            items.append((k, k, "Rename bones for %s" % k))
    return tuple(items)

bpy.types.Scene.ssb4_character = bpy.props.EnumProperty(
    name="Active Fighter",
    description="Select the active fighter to determine character specific bone mapping",
    items=character_items_scene)

bpy.types.Scene.ssb4_scope = bpy.props.EnumProperty(
    name="Target Bones:",
    description="Apply operations to selected bones or all bones in the armature",
    items=(("ALL", "All", "Operate on ALL bones in the armature"),
           ("SELECTED", "Selected", "Operate only on selected bones")), default="ALL")

bpy.types.Scene.ssb4_rename_swing = bpy.props.BoolProperty(
    name="Convert Swing Bones",
    description="Enable renaming unmapped swing bones (SWG_*__swing  ↔  S_*)",
    default=True)

bpy.types.Scene.ssb4_rename_null_swing = bpy.props.BoolProperty(
    name="Convert Null Bones",
    description="Enable renaming unmapped null-swing bones (SWG_*__shit  ↔  S_*_null)",
    default=True)

bpy.types.Scene.ssb4_find = bpy.props.StringProperty(
    name="Find", description="Text matching this in bone names will be replaced with the text in 'Replace'", default="")

bpy.types.Scene.ssb4_replace = bpy.props.StringProperty(
    name="Replace", description="Text to replace the 'Find' match with", default="")

bpy.types.Scene.ssb4_direction = bpy.props.EnumProperty(
    name="Direction",
    description="Choose whether to trim from the START or END of the bone name",
    items=(("START", "Start", "Trim characters from the START of the bone name"),
           ("END", "End", "Trim characters from the END of the bone name")), default="START")

bpy.types.Scene.ssb4_count = bpy.props.IntProperty(
    name="Count", default=1, description="Numerical value used during trimming")

bpy.types.Scene.ssb4_trim_count = bpy.props.IntProperty(
    name="Trim Count", default=3, description="Number of characters to trim from the end of bone names")

bpy.types.Scene.ssb4_clear_hip = bpy.props.BoolProperty(
    name="Clear Hip", description="When enabled, hip bones (Hip/HipN) are not immune to location keyframe clearing.", default=False)

bpy.types.Scene.ssb4_override_lock = bpy.props.BoolProperty(
    name="Override Lock",
    description="Bypass the ultibones/revert poll restriction",
    default=False)

# ------------------------------------------------------------
# Track last naming scheme: SSB4, SSBU, or Valve
# ------------------------------------------------------------
bpy.types.Scene.ssb4_last_scheme = bpy.props.EnumProperty(
    name="Last Naming Scheme",
    items=[
        ('SSB4', 'SSB4', ''),
        ('SSBU', 'SSBU', ''),
        ('Valve', 'Valve', ''),
    ], default='SSBU')

# ------------------------------------------------------------
# Functions
# ------------------------------------------------------------

def load_custom_icons():
    global custom_icons
    custom_icons = bpy.utils.previews.new()
    addon_dir = os.path.dirname(__file__)
    icon_path = os.path.join(addon_dir, "icon.png")
    custom_icons.load("SMASH_ICON", icon_path, 'IMAGE')

def unload_custom_icons():
    global custom_icons
    bpy.utils.previews.remove(custom_icons)
    custom_icons = None
    
def get_target_bones(armature):
    """Checks the toggle setting to determine whether to operate on all bones or only selected."""
    scope = bpy.context.scene.ssb4_scope
    if scope == 'SELECTED':
        return [b for b in armature.bones if getattr(b, 'select', False)]
    return list(armature.bones)

def normalize_bone_name(name):
    """Strip 'ValveBiped.' prefix from index 0 of the bonemap, otherwise return name unchanged."""
    if name.startswith("ValveBiped."):
        return name[len("ValveBiped."):]
    return name

def get_bone_name_set(bones):
    """Return a set of every bone.name plus its normalized form."""
    names = set()
    for b in bones:
        names.add(b.name)
        names.add(normalize_bone_name(b.name))
    return names

def build_bone_map(maps, fmt_i):
    """
    Build your lookup dict. Keys are every src and its stripped form.
    Values are the *raw* entry[fmt_i] (with ValveBiped. still on it for index 0).
    """
    bone_map = {}
    for entry in maps:
        raw_tgt = entry[fmt_i]  # DON'T normalize here
        for src in entry:
            bone_map[src] = raw_tgt
            stripped = normalize_bone_name(src)
            if stripped != src:
                bone_map[stripped] = raw_tgt
    return bone_map


# Primary renaming function
# ------------------------------------------------------------
def rename_bones(character, target_format='SSBU', ignore_scope=False):
    """
    Unified renamer for all formats:
      - direct-map any bone in CHARACTER_BONE_MAPS
      - otherwise swap swing/null-swing based strictly on parent’s current name:
         • SSBU→SSB4 null-swing: take S_…_null, look at parent’s SWG_…__swing,
           bump its number by +1, apply SWG_…__shit.
         • SSB4→SSBU null-swing: take SWG_…__shit, look at parent’s S_…,
           bump its number by +1, apply S_…_null.
         • HL2/TF2: mirror the same bump logic, then map via bone_map to Valve names.
      - all within one pass, parents always processed before children by depth sort
      - controlled by scene.ssb4_rename_swing & scene.ssb4_rename_null_swing
    """
    scene = bpy.context.scene
    do_swing = scene.ssb4_rename_swing
    do_null_swing = scene.ssb4_rename_null_swing

    obj = scene.objects.active
    if not obj or obj.type != 'ARMATURE':
        print("ERROR: No armature selected!")
        return False, 0, ""
    armature = obj.data

    # Collect target bones
    bones = list(armature.bones) if ignore_scope else get_target_bones(armature)

    # Sort by hierarchy depth so parents rename before children
    def depth(b):
        d = 0
        cur = b
        while cur.parent:
            d += 1
            cur = cur.parent
        return d
    bones.sort(key=depth)

    # --- Build Direct Map ---
    common_clav, common_leg, common_list = CHARACTER_BONE_MAPS.get("Common", (False, False, []))
    char_clav, char_leg, char_list = CHARACTER_BONE_MAPS.get(character, (False, False, []))
    maps = common_list + char_list
    fmt_i = {'HL2': 0, 'TF2': 1, 'SSB4': 2, 'SSBU': 3}[target_format]
    prefs = bpy.context.user_preferences.addons[__name__].preferences

    bone_map = build_bone_map(maps, fmt_i)

    DIGITS = re.compile(r'^(.*?)(\d+)$')
    renamed = 0

    for bone in bones:
        orig = bone.name
        new = None

        # 1) Direct Map?
        if orig in bone_map:
            new = bone_map[orig]
            # Honor the user’s Valve naming preferences
            if target_format in ('HL2','TF2') and prefs.trim_valvebiped:
                new = normalize_bone_name(new)

        else:
            # 2) to SSB4?
            if target_format == 'SSB4':
                if do_null_swing and orig.startswith("S_") and orig.endswith("_null"):
                    p = bone.parent
                    if p and p.name.startswith("SWG_") and p.name.endswith("__swing"):
                        core = p.name[4:-7]
                        m = DIGITS.match(core)
                        if m:
                            pre, num = m.groups()
                            core = pre + str(int(num) + 1)
                        new = "SWG_%s__shit" % core
                    else:
                        core = orig[2:-5]
                        new = "SWG_%s__shit" % core
                elif do_swing and orig.startswith("S_"):
                    core = orig[2:]
                    new = "SWG_%s__swing" % core

            # 3) to SSBU?
            elif target_format == 'SSBU':
                if do_null_swing and orig.startswith("SWG_") and orig.endswith("__shit"):
                    p = bone.parent
                    if p and p.name.startswith("S_"):
                        core = p.name[2:]
                        if core.endswith("_null"):
                            core = core[:-5]
                        m = DIGITS.match(core)
                        if m:
                            pre, num = m.groups()
                            core = pre + str(int(num) + 1)
                        new = "S_%s_null" % core
                    if not new:
                        core = orig[4:-6]
                        new = "S_%s_null" % core
                elif do_swing and orig.startswith("SWG_") and orig.endswith("__swing"):
                    core = orig[4:-7]
                    new = "S_%s" % core

            # 4) to Valve (HL2/TF2)?
            elif target_format in ('HL2', 'TF2') and do_null_swing:
                # SSBU-style null → bump off SWG parent and lookup swing
                if orig.startswith("S_") and orig.endswith("_null"):
                    p = bone.parent
                    if p and p.name.startswith("SWG_") and p.name.endswith("__swing"):
                        core = p.name[4:-7]
                        m = DIGITS.match(core)
                        if m:
                            pre, num = m.groups()
                            core = pre + str(int(num) + 1)
                        swing_name = "SWG_%s__swing" % core
                    else:
                        core = orig[2:-5]
                        swing_name = "SWG_%s__swing" % core
                    new = bone_map.get(swing_name)

                # SSB4-style null → bump off S_ parent and lookup null
                elif orig.startswith("SWG_") and orig.endswith("__shit"):
                    p = bone.parent
                    if p and p.name.startswith("S_"):
                        core = p.name[2:]
                        if core.endswith("_null"):
                            core = core[:-5]
                        m = DIGITS.match(core)
                        if m:
                            pre, num = m.groups()
                            core = pre + str(int(num) + 1)
                        null_name = "S_%s_null" % core
                    else:
                        core = orig[4:-6]
                        null_name = "S_%s_null" % core
                    new = bone_map.get(null_name)

            # 5) fallback swing
            if not new and do_swing and orig.startswith("S_"):
                core = orig[2:]
                new = bone_map.get("SWG_%s__swing" % core)

        # Apply rename
        if new and new != orig:
            bone.name = new
            renamed += 1

    # set last_scheme to fit Enum
    if target_format in ('HL2', 'TF2'):
        scene.ssb4_last_scheme = 'Valve'
    else:
        scene.ssb4_last_scheme = target_format

    print("Renamed %d bones for '%s' (to %s) on %s!" %
          (renamed, character, target_format, obj.name))
    return True, renamed, obj.name

# ------------------------------------------------------------
# Operators for Automatic Renaming between SSB4 and SSBU
# ------------------------------------------------------------
class SSB_OT_ConvertToSSBU(bpy.types.Operator):
    bl_idname = "ssb4.convert_to_ssbu"
    bl_label = "Convert to SSBU"
    bl_description = ("Convert bones to SSBU naming conventions and automatically add missing bones")

    character = bpy.props.EnumProperty(
        name="Active Fighter",
        description="Select which fighter's bone map to use",
        items=character_items_scene)
    force = bpy.props.BoolProperty(name="Force Rename", default=False)

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        # Warn if a smash-ultimate-blender conversion is active on this armature
        obj = context.scene.objects.active
        arm_name = obj.name if obj else "<none>"
        layout.label(text="smash-ultimate-blender conversion is active on this armature!  Rename anyway?", icon="ERROR")

    def invoke(self, context, event):
        if not context.scene.ssb4_character:
            context.scene.ssb4_character = "Common"
        self.character = context.scene.ssb4_character

        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}

        arm_key = obj.as_pointer()
        if arm_key in ORIGINAL_BONE_DATA and not self.force:
            return context.window_manager.invoke_props_dialog(self, width=490)

        return self.execute(context)

    def execute(self, context):
        obj = context.scene.objects.active
        # Store initial mode (OBJECT, EDIT, or POSE) to restore later
        init_mode = obj.mode if obj else 'OBJECT'

        # First, rename the SSB4 bones to SSBU
        result, count, arm_name = rename_bones(
            self.character,
            target_format='SSBU',
            ignore_scope=False)
        if not result:
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}

        # Remember scheme
        context.scene.ssb4_last_scheme = 'SSBU'
        self.report({'INFO'}, "Renamed %d bones to SSBU on %s" % (count, arm_name))

        # Now add extra bones
        extra = []
        bpy.ops.object.mode_set(mode='EDIT')
        arm = obj.data
        edit_bones = arm.edit_bones

        # Decide from the bonemap whether this character uses the ClavicleC bone
        _, _, char_list = CHARACTER_BONE_MAPS.get("Common", (False, False, []))
        enable_clavicleC, enable_legC, _ = CHARACTER_BONE_MAPS.get(self.character, (False, False, []))

        # Add ClavicleC *only* if the flag is True
        if enable_clavicleC and "ClavicleL" in edit_bones and "ClavicleR" in edit_bones:
            clavL = edit_bones["ClavicleL"]
            clavR = edit_bones["ClavicleR"]
            bust = edit_bones.get("Bust") or edit_bones.get("BustN")
            if bust and "ClavicleC" not in edit_bones:
                clavC = edit_bones.new("ClavicleC")
                # Position: head is midway between ClavicleL.head and ClavicleR.head.
                clavC.head = (clavL.head + clavR.head) / 2.0
                # Direction from ClavicleR.head to ClavicleR.tail
                dir_vec = (clavR.tail - clavR.head).normalized()
                # Tail is head + direction * ClavicleR.length
                clavC.tail = clavC.head + dir_vec * clavR.length
                # Set the bone's roll to the *median* (average) of ClavicleL.roll and ClavicleR.roll
                clavC.roll = 0.5 * (clavL.roll + clavR.roll)
                # Explicitly set length
                clavC.length = (clavC.tail - clavC.head).length
                # Re-parent ClavicleL and ClavicleR to ClavicleC, parent ClavicleC to Bust
                clavL.parent = clavC
                clavR.parent = clavC
                clavC.parent = bust
                extra.append("ClavicleC")

        # Add LegC *only* if the flag is True
        if enable_legC and "LegL" in edit_bones and "LegR" in edit_bones:
            legL = edit_bones["LegL"]
            legR = edit_bones["LegR"]
            hip  = edit_bones.get("Hip") or edit_bones.get("HipN")
            if hip and "LegC" not in edit_bones:
                legC = edit_bones.new("LegC")
                # Set head exactly between the TAILS of LegL and LegR
                legC.head = (legL.tail + legR.tail) / 2.0
                # Compute direction from LegR.head to LegR.tail
                dir_vec = (legR.tail - legR.head).normalized()
                # Initially set tail as head plus (direction * legR.length)
                legC.tail = legC.head + dir_vec * legR.length
                # Flip the bone by inverting the vector from head to tail
                vec = legC.tail - legC.head
                legC.tail = legC.head - vec  # This rotates legC 180° around its head
                # Copy roll from LegR
                legC.roll = legR.roll
                # Set length explicitly
                legC.length = (legC.tail - legC.head).length
                # Re-parent LegL and LegR to LegC, parent LegC to Hip
                legL.parent = legC
                legR.parent = legC
                legC.parent = hip
                extra.append("LegC")

        # Restore the original mode
        bpy.ops.object.mode_set(mode=init_mode)

        # Only report extra‐bones message if we actually created any
        if extra:
            self.report({'INFO'},"Converted to SSBU on %s; added %s." % (arm_name, " and ".join(extra)))
        return {'FINISHED'}

class SSB_OT_ConvertToSSB4(bpy.types.Operator):
    bl_idname = "ssb4.convert_to_ssb4"
    bl_label = "Convert to SSB4"
    bl_description = "Convert bones to SSB4 naming conventions"

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    character = bpy.props.EnumProperty(
        name="Active Fighter",
        description="Select which fighter's bone map to use",
        items=character_items_scene)

    def draw(self, context):
        layout = self.layout
        # Warn if a smash-ultimate-blender conversion is active on this armature
        layout.label(text="smash-ultimate-blender conversion is active on this armature!  Rename anyway?", icon="ERROR")

    def invoke(self, context, event):
        obj = context.scene.objects.active
        # If smash-ultimate-blender conversion active, show a wider props dialog
        if obj and obj.as_pointer() in ORIGINAL_BONE_DATA:
            return context.window_manager.invoke_props_dialog(self, width=490)
        if not context.scene.ssb4_character:
            context.scene.ssb4_character = "Common"
        self.character = context.scene.ssb4_character
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}
        return self.execute(context)

    def execute(self, context):
        # Rename bones to SSB4 according to the helper
        ok, count, arm_name = rename_bones(
            self.character,
            target_format='SSB4',
            ignore_scope=False)
        if not ok:
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}

        # Remember scheme
        context.scene.ssb4_last_scheme = 'SSB4'
        self.report({'INFO'}, "Renamed %d bones to SSB4 on %s" % (count, arm_name))
        return {'FINISHED'}

# ------------------------------------------------------------
# Operator: Rename to Valve (HL2 or TF2)
# ------------------------------------------------------------
class SSB_OT_ConvertToValve(bpy.types.Operator):
    bl_idname = 'ssb4.convert_to_valve'
    bl_label = 'Convert to Valve'
    bl_description = (
        'Convert bones to Valve biped naming conventions, '
        'or generate a QC + $bonemerge/$renamebone script')

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def execute(self, context):
        scene = context.scene
        prefs = bpy.context.user_preferences.addons[__name__].preferences
        fmt = prefs.valve_bone_format
        obj = scene.objects.active
        arm = obj.data
        bones = get_target_bones(arm)

        bone_names = get_bone_name_set(bones)

        # Rename bones mode
        if not prefs.convert_to_valve_script:
            ok, count, _ = rename_bones(
                scene.ssb4_character,
                target_format=fmt,
                ignore_scope=False
            )
            if not ok:
                self.report({'ERROR'}, "Valve renaming failed on %s" % obj.name)
                return {'CANCELLED'}
            scene.ssb4_last_scheme = 'Valve'
            self.report({'INFO'}, "Renamed %d bones to %s on %s" % (
                count, fmt, obj.name
            ))
            return {'FINISHED'}

        # Script output mode → Prepare text block
        names = get_bone_name_set(bones)
        text_name = obj.name + "_$renamebones"
        txt = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
        txt.clear()

        separator = "//----------------------------------------------\n"

        # Buffers for conditional QC sections
        opt_lines = []
        leg_lines = []
        shoulder_lines = []
        hand_lines = []

        # 2) Set root
        for root in ('HipN', 'Hip', 'bip_pelvis', 'ValveBiped.Bip01_Pelvis'):
            if root in names:
                opt_lines.append("// Set root bone to pelvis\n")
                opt_lines.append("$hierarchy %s%s\"\"\n" % (root, ' ' * 3))
                opt_lines.append("$root %s\n\n" % root)
                break

        # 3) Collapse old roots
        old_root_names = ['Trans', 'Throw', 'Rot', 'TransN', 'ThrowN', 'RotN']
        old_roots = [r for r in old_root_names if r in names]
        if old_roots:
            max_len = max(len(r) for r in old_roots)
            opt_lines.append("// Collapse old roots\n")
            for r in old_roots:
                opt_lines.append("$alwayscollapse %s%s\n" % (r, ' ' * (max_len - len(r) + 3)))
            opt_lines.append("\n")

        # 4) Reorganize leg hierarchy
        leg_sets = [
            ('LLegJ', 'HipN'), ('RLegJ', 'HipN'),
            ('LegL', 'Hip'), ('LegR', 'Hip'),
            ('bip_hip_L', 'bip_pelvis'), ('bip_hip_R', 'bip_pelvis'),
            ('ValveBiped.Bip01_L_Thigh', 'ValveBiped.Bip01_Pelvis'),
            ('ValveBiped.Bip01_R_Thigh', 'ValveBiped.Bip01_Pelvis'),
        ]
        leg_pairs = [(b, p) for (b, p) in leg_sets if b in names and p in names]
        if leg_pairs:
            leg_lines.append(separator + "\n")
            leg_lines.append("// Reorganize leg hierarchy\n")
            max_len = max(len(b) for b, p in leg_pairs)
            for b, p in leg_pairs:
                leg_lines.append("$hierarchy %s%s%s\n" % (b, ' ' * (max_len - len(b) + 3), p))
            leg_lines.append("\n")
            for leg_c in ('CLegJ', 'LegC', 'bip_hip_C', 'ValveBiped.Bip01_C_Thigh'):
                if leg_c in names:
                    leg_lines.append("// Collapse leg root\n")
                    leg_lines.append("$alwayscollapse %s\n\n" % leg_c)
                    break

        # 5) Reorganize shoulder hierarchy
        shoulder_sets = [
            ('LShoulderN', 'BustN'), ('RShoulderN', 'BustN'),
            ('ClavicleL', 'Bust'), ('ClavicleR', 'Bust'),
            ('bip_collar_L', 'bip_spine_1'), ('bip_collar_R', 'bip_spine_1'),
            ('ValveBiped.Bip01_L_Clavicle', 'ValveBiped.Bip01_Spine1'),
            ('ValveBiped.Bip01_R_Clavicle', 'ValveBiped.Bip01_Spine1'),
        ]
        shoulder_pairs = [(b, p) for (b, p) in shoulder_sets if b in names and p in names]
        if shoulder_pairs:
            shoulder_lines.append(separator + "\n")
            shoulder_lines.append("// Reorganize shoulder hierarchy\n")
            max_len = max(len(b) for b, p in shoulder_pairs)
            for b, p in shoulder_pairs:
                shoulder_lines.append("$hierarchy %s%s%s\n" % (b, ' ' * (max_len - len(b) + 3), p))
            shoulder_lines.append("\n")
            for root in ('CShoulderN', 'ClavicleC', 'bip_spine_2', 'ValveBiped.Bip01_Spine2'):
                if root in names:
                    shoulder_lines.append("// Collapse clavicle root\n")
                    shoulder_lines.append("$alwayscollapse %s\n\n" % root)
                    break

        # 6) Reorganize hand + Collapse carpal finger bones
        finger_roots = [
            f for f in [
                'LFingerBaseN','LMiddleN','LRingN','LPinkyN',
                'RFingerBaseN','RMiddleN','RRingN','RPinkyN',
                'FingerL10','FingerL20','FingerL30','FingerL40',
                'FingerR10','FingerR20','FingerR30','FingerR40',
                'bip_index_carpal_L','bip_middle_carpal_L','bip_ring_carpal_L','bip_pinky_carpal_L',
                'bip_index_carpal_R','bip_middle_carpal_R','bip_ring_carpal_R','bip_pinky_carpal_R',
                'ValveBiped.Bip01_L_Finger1_Carpal','ValveBiped.Bip01_L_Finger2_Carpal',
                'ValveBiped.Bip01_L_Finger3_Carpal','ValveBiped.Bip01_L_Finger4_Carpal',
                'ValveBiped.Bip01_R_Finger1_Carpal','ValveBiped.Bip01_R_Finger2_Carpal',
                'ValveBiped.Bip01_R_Finger3_Carpal','ValveBiped.Bip01_R_Finger4_Carpal'
            ] if f in names
        ]

        collapse_map = {
            'LIndex1N':'LFingerBaseN','LThumb1N':'LFingerBaseN',
            'LMiddle1N':'LMiddleN','LRing1N':'LRingN','LPinky1N':'LPinkyN',
            'RIndex1N':'RFingerBaseN','RThumb1N':'RFingerBaseN',
            'RMiddle1N':'RMiddleN','RRing1N':'RRingN','RPinky1N':'RPinkyN',
            'FingerL11':'FingerL10','FingerL51':'FingerL10',
            'FingerL21':'FingerL20','FingerL31':'FingerL30','FingerL41':'FingerL40',
            'FingerR11':'FingerR10','FingerR51':'FingerR10',
            'FingerR21':'FingerR20','FingerR31':'FingerR30','FingerR41':'FingerR40',
            'bip_index_0_L':'bip_index_carpal_L','bip_middle_0_L':'bip_middle_carpal_L',
            'bip_ring_0_L':'bip_ring_carpal_L','bip_pinky_0_L':'bip_pinky_carpal_L',
            'bip_thumb_0_L':'bip_index_carpal_L','bip_index_0_R':'bip_index_carpal_R',
            'bip_middle_0_R':'bip_middle_carpal_R','bip_ring_0_R':'bip_ring_carpal_R',
            'bip_pinky_0_R':'bip_pinky_carpal_R','bip_thumb_0_R':'bip_index_carpal_R',
            'ValveBiped.Bip01_L_Finger1':'ValveBiped.Bip01_L_Finger1_Carpal',
            'ValveBiped.Bip01_L_Finger2':'ValveBiped.Bip01_L_Finger2_Carpal',
            'ValveBiped.Bip01_L_Finger3':'ValveBiped.Bip01_L_Finger3_Carpal',
            'ValveBiped.Bip01_L_Finger4':'ValveBiped.Bip01_L_Finger4_Carpal',
            'ValveBiped.Bip01_L_Finger0':'ValveBiped.Bip01_L_Finger1_Carpal',
            'ValveBiped.Bip01_R_Finger1':'ValveBiped.Bip01_R_Finger1_Carpal',
            'ValveBiped.Bip01_R_Finger2':'ValveBiped.Bip01_R_Finger2_Carpal',
            'ValveBiped.Bip01_R_Finger3':'ValveBiped.Bip01_R_Finger3_Carpal',
            'ValveBiped.Bip01_R_Finger4':'ValveBiped.Bip01_R_Finger4_Carpal',
            'ValveBiped.Bip01_R_Finger0':'ValveBiped.Bip01_R_Finger1_Carpal',
        }

        hand_map = [
            ('LIndex1N','LHandN'),('LThumb1N','LHandN'),
            ('LMiddle1N','LHandN'),('LRing1N','LHandN'),('LPinky1N','LHandN'),
            ('RIndex1N','RHandN'),('RThumb1N','RHandN'),
            ('RMiddle1N','RHandN'),('RRing1N','RHandN'),('RPinky1N','RHandN'),
            ('FingerL11','HandL'),('FingerL51','HandL'),
            ('FingerL21','HandL'),('FingerL31','HandL'),('FingerL41','HandL'),
            ('FingerR11','HandR'),('FingerR51','HandR'),
            ('FingerR21','HandR'),('FingerR31','HandR'),('FingerR41','HandR'),
            ('bip_index_0_L','bip_hand_L'),('bip_middle_0_L','bip_hand_L'),
            ('bip_ring_0_L','bip_hand_L'),('bip_pinky_0_L','bip_hand_L'),('bip_thumb_0_L','bip_hand_L'),
            ('bip_index_0_R','bip_hand_R'),('bip_middle_0_R','bip_hand_R'),
            ('bip_ring_0_R','bip_hand_R'),('bip_pinky_0_R','bip_hand_R'),('bip_thumb_0_R','bip_hand_R'),
            ('ValveBiped.Bip01_L_Finger1','ValveBiped.Bip01_L_Hand'),
            ('ValveBiped.Bip01_L_Finger0','ValveBiped.Bip01_L_Hand'),
            ('ValveBiped.Bip01_L_Finger2','ValveBiped.Bip01_L_Hand'),
            ('ValveBiped.Bip01_L_Finger3','ValveBiped.Bip01_L_Hand'),
            ('ValveBiped.Bip01_L_Finger4','ValveBiped.Bip01_L_Hand'),
            ('ValveBiped.Bip01_R_Finger1','ValveBiped.Bip01_R_Hand'),
            ('ValveBiped.Bip01_R_Finger0','ValveBiped.Bip01_R_Hand'),
            ('ValveBiped.Bip01_R_Finger2','ValveBiped.Bip01_R_Hand'),
            ('ValveBiped.Bip01_R_Finger3','ValveBiped.Bip01_R_Hand'),
            ('ValveBiped.Bip01_R_Finger4','ValveBiped.Bip01_R_Hand'),
        ]
        valid_hand_map = [
            (b, p) for b, p in hand_map
            if b in names and p in names and collapse_map.get(b) in finger_roots
        ]
        if valid_hand_map:
            hand_lines.append(separator + "\n")
            hand_lines.append("// Reorganize hand hierarchy\n")
            max_len = max(len(b) for b, p in valid_hand_map)
            for b, p in valid_hand_map:
                hand_lines.append("$hierarchy %s%s%s\n" % (b, ' ' * (max_len - len(b) + 3), p))
            hand_lines.append("\n")

        if finger_roots:
            hand_lines.append("// Collapse carpal finger bones\n")
            max_len = max(len(f) for f in finger_roots)
            for f in finger_roots:
                hand_lines.append("$alwayscollapse %s%s\n" % (f, ' ' * (max_len - len(f) + 3)))
            hand_lines.append("\n")

        # Write OPTIONAL OPTIMIZATIONS if any of the 4 sections are non-empty
        if opt_lines or leg_lines or shoulder_lines or hand_lines:
            txt.write(separator)
            txt.write("// OPTIONAL OPTIMIZATIONS (state at the top in this order, should not break animations)\n")
            txt.write(separator + "\n")
            for line in opt_lines + leg_lines + shoulder_lines + hand_lines:
                txt.write(line)

        # 7) $bonemerge section
        bonemerge_lines = ["$bonemerge %s\n" % b.name for b in bones if not b.children]
        if bonemerge_lines:
            txt.write(separator)
            txt.write("// BONEMERGE (all leaf bones, prevents removal during compile)\n")
            txt.write(separator + "\n")
            for line in bonemerge_lines:
                txt.write(line)
            txt.write("\n")

        # 8) $renamebone section, grouped & aligned using Blender bone groups
        from .bonemaps import CHARACTER_BONE_MAPS
        common_clav, common_leg, common_list = CHARACTER_BONE_MAPS["Common"]
        char_clav,   char_leg,   char_list   = CHARACTER_BONE_MAPS[scene.ssb4_character]
        all_maps = common_list + char_list

        bone_map = build_bone_map(all_maps, fmt_i)

        pairs = [
            (b.name, bone_map[b.name]) for b in bones
            if bone_map.get(b.name) and b.name != bone_map[b.name]
        ]

        if pairs:
            max_len = max(len(o) for o, _ in pairs)
            group_dict = {}
            for old,new in pairs:
                pb = obj.pose.bones.get(old)
                group = pb.bone_group.name if pb and pb.bone_group else "<No Group>"
                group_dict.setdefault(group, []).append((old,new))

            txt.write(separator)
            txt.write("// RENAME BONES (by bone groups)\n")
            txt.write(separator + "\n")
            for group in obj.pose.bone_groups:
                entries = group_dict.get(group.name, [])
                if not entries:
                    continue
                txt.write("// %s\n" % group.name)
                for o,nw in sorted(entries):
                    txt.write("$renamebone %s%s%s\n" % (o, ' ' * (max_len - len(o) + 3), nw))
                txt.write("\n")
            if "<No Group>" in group_dict:
                txt.write("// Ungrouped\n")
                for o,nw in sorted(group_dict["<No Group>"]):
                    txt.write("$renamebone %s%s%s\n" % (o, ' ' * (max_len - len(o) + 3), nw))
                txt.write("\n")

        # Reveal in Text Editor
        for area in context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces.active.text = txt

        self.report({'INFO'}, "Generated Valve‐QC script for %s" % obj.name)
        return {'FINISHED'}

# ------------------------------------------------------------
# Operator for Trimming Bone Names
# ------------------------------------------------------------
class SSB_OT_TrimString(bpy.types.Operator):
    bl_idname = "ssb4.trim_string"
    bl_label = "TRIM"
    bl_description = "Trim the specified number of characters from the START or END of bone names"

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        # Warn if smash-ultimate-blender conversion is active on this armature
        obj = context.scene.objects.active
        arm_name = obj.name if obj else "<none>"
        layout.label(text="smash-ultimate-blender conversion is active on this armature!  Trim anyway?", icon="ERROR")

    def invoke(self, context, event):
        obj = context.scene.objects.active
        if obj and obj.as_pointer() in ORIGINAL_BONE_DATA:
            return context.window_manager.invoke_props_dialog(self, width=470)
        return self.execute(context)

    def execute(self, context):
        trim_count = context.scene.ssb4_count  # Use the property shown in the UI
        direction = context.scene.ssb4_direction  # Either "START" or "END"
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}
        armature = obj.data
        for bone in get_target_bones(armature):
            if len(bone.name) > trim_count:
                if direction == "START":
                    bone.name = bone.name[trim_count:]
                else:  # Direction == "END"
                    bone.name = bone.name[:-trim_count]
        self.report(
            {'INFO'},
            "Trimmed bone names by %d characters from the %s." %
            (trim_count, direction.lower()))
        return {'FINISHED'}

# ------------------------------------------------------------
# Operator for Find/Replace on Bones
# ------------------------------------------------------------
class SSB_OT_FindReplaceBones(bpy.types.Operator):
    bl_idname = "ssb4.find_replace_bones"
    bl_label = "Find/Replace Bones"
    bl_description = "Replace specified text within bone names"

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        obj = context.scene.objects.active
        arm_name = obj.name if obj else "<none>"
        layout.label(text="smash-ultimate-blender conversion is active on this armature!  Rename anyway?", icon="ERROR")

    def invoke(self, context, event):
        obj = context.scene.objects.active
        # If smash-ultimate-blender conversion is active, show a warning
        if obj and obj.as_pointer() in ORIGINAL_BONE_DATA:
            return context.window_manager.invoke_props_dialog(self, width=490)
        return self.execute(context)

    def execute(self, context):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}
        armature = obj.data
        target_bones = get_target_bones(armature)
        find_str = context.scene.ssb4_find
        replace_str = context.scene.ssb4_replace
        if not find_str:
            self.report({'ERROR'}, "Find string is empty!")
            return {'CANCELLED'}

        # Perform find-replace and count actual renames
        renamed = 0
        for bone in target_bones:
            old_name = bone.name
            new_name = old_name.replace(find_str, replace_str)
            if old_name != new_name:
                bone.name = new_name
                renamed += 1

        self.report({'INFO'}, "Renamed %d bones on %s" % (renamed, obj.name))
        return {'FINISHED'}

# ------------------------------------------------------------
# Strip .NUANMX (Action Renamer)
# ------------------------------------------------------------
class SSB_OT_StripNuanmxFromActions(bpy.types.Operator):
    bl_idname = "ssb4.strip_nuanmx"
    bl_label = "Strip .nuanmx"
    bl_description = "Removes '.nuanmx' from all action names in the scene"

    def execute(self, context):
        count = 0
        for action in bpy.data.actions:
            if ".nuanmx" in action.name:
                action.name = action.name.replace(".nuanmx", "")
                count += 1
        self.report({'INFO'}, "Stripped '.nuanmx' from {} actions.".format(count))
        return {'FINISHED'}

# ------------------------------------------------------------
# Operator for Rotating Root Bone 90° on X
# ------------------------------------------------------------
class SSB_OT_Rotate90(bpy.types.Operator):
    """
    Bakes a 90° X-axis rotation into the active action by duplicating the armature,
    constraining the original to the rotated duplicate, baking visual keyframes, and cleaning up.
    """
    bl_idname = "ssb4.rotate_trans90"
    bl_label = "Rotate Action Y 90 Bake"
    bl_description = ("Rotates the current action up 90° on the global X axis")

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def execute(self, context):
        # STEP 1: Duplicate the active armature.
        target_obj = context.scene.objects.active
        if not target_obj or target_obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}
        self.report({'INFO'}, "Step 1: Duplicating armature '%s'" % target_obj.name)
        dup_obj = target_obj.copy()
        dup_obj.data = target_obj.data.copy()
        context.scene.objects.link(dup_obj)
        
        # STEP 2: Add copy transforms constraints on every pose bone of the original.
        constraints_list = []
        for pb in target_obj.pose.bones:
            constr = pb.constraints.new('COPY_TRANSFORMS')
            constr.name = "Temp_CopyTransform"
            constr.target = dup_obj
            constr.subtarget = pb.name
            constraints_list.append((pb, constr))
        self.report({'INFO'}, "Step 2: Added copy transforms constraints on %d bones" % len(constraints_list))
        
        # STEP 3: Rotate the duplicate 90° on the global X axis from the world origin.
        # To rotate about the world origin, pre-multiply the duplicate's matrix_world.
        R = Matrix.Rotation(math.radians(90), 4, 'X')
        dup_obj.matrix_world = R * dup_obj.matrix_world
        self.report({'INFO'}, "Step 3: Rotated duplicate '%s' 90° on global X" % dup_obj.name)
        
        # STEP 4: Bake the original armature's action using visual keying.
        start_frame = context.scene.frame_start
        end_frame = context.scene.frame_end
        self.report({'INFO'}, "Step 4: Baking action from frame %d to %d" % (start_frame, end_frame))
        bpy.ops.nla.bake(
            frame_start=start_frame,
            frame_end=end_frame,
            only_selected=False,
            visual_keying=True,
            clear_constraints=False,  # constraints remain for baking
            clear_parents=False,
            use_current_action=True,
            bake_types={'POSE'}
        )
        
        # STEP 5: Remove the temporary constraints and delete the duplicate armature.
        for pb, constr in constraints_list:
            pb.constraints.remove(constr)
        self.report({'INFO'}, "Step 5a: Removed temporary constraints from original armature")
        context.scene.objects.unlink(dup_obj)
        bpy.data.objects.remove(dup_obj)
        self.report({'INFO'}, "Step 5b: Deleted duplicate armature")
        
        self.report({'INFO'}, "Finished baking 90° X rotation into the action.")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return self.execute(context)

# ------------------------------------------------------------
# Operator for Pose Bone Transforms (re-targeting via Copy Constraints)
# ------------------------------------------------------------
class SSB_OT_PoseBoneTransforms(bpy.types.Operator):
    """
    Applies copy transforms constraints from one armature to another based on matching bone names.
    This operator applies a specific type of constraint (rotation, location, scale, or transform)
    to matching bones between the target armature (the active armature) and the source armature
    selected by the user. The constraints copy transforms from the source armature to the target armature.
    If the "Apply as Visual Transform" option is selected, the constraints will be applied to the 
    target armature, followed by a visual transform application. All copy constraints from the source
    armature will then be removed from the target armature.
    """
    bl_idname = "pose.copy_transforms_from_other"
    bl_label = "Copy Transforms From Other Armature"
    bl_description = 'Copy transforms on matching bone names from another armature in the scene'
    bl_options = {'REGISTER', 'UNDO'}

    source_armature = bpy.props.EnumProperty(
        items=lambda self, context: [
            (obj.name, obj.name, "") for obj in bpy.data.objects
            if obj.type == 'ARMATURE' and obj != SSB_OT_PoseBoneTransforms.get_target_armature(context)], name="Source")

    constraint_type = bpy.props.EnumProperty(
        name="Constraint Type",
        items=[
            ('COPY_ROTATION',   'Copy Rotation',    ''),
            ('COPY_LOCATION',   'Copy Location',    ''),
            ('COPY_SCALE',      'Copy Scale',       ''),
            ('COPY_TRANSFORMS', 'Copy Transforms',  ''),
        ],)

    apply_visual_transform = bpy.props.BoolProperty(
        name="Apply as Visual Transform",
        description="Apply visual transforms to pose and clear created constraints",
        default=False)

    only_selected = bpy.props.BoolProperty(
        name="Only Selected",
        description="Apply constraints only to the selected bones",
        default=False)

    clear_previous = bpy.props.BoolProperty(
        name="Clear Previous",
        description="Clear existing COPY constraints (rotation/location/scale/transforms) before applying new ones",
        default=False)

    @classmethod
    def get_target_armature(cls, context):
        obj = context.active_object
        if obj and obj.type == 'ARMATURE' and obj.mode == 'POSE':
            return obj
        if obj and obj.type == 'MESH' and context.mode == 'PAINT_WEIGHT':
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object:
                    return mod.object
        return None

    @classmethod
    def poll(cls, context):
        return bool(cls.get_target_armature(context))

    def invoke(self, context, event):
        target = self.get_target_armature(context)
        if not any(o.type == 'ARMATURE' and o != target for o in bpy.data.objects):
            self.report({'ERROR'}, "No other armatures found in the scene.")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def clear_constraints(self, bones, source_armature):
        for bone in bones:
            for c in [c for c in bone.constraints
                      if c.type.startswith('COPY_') and c.target == source_armature]:
                bone.constraints.remove(c)

    def execute(self, context):
        target = self.get_target_armature(context)
        if not target or not self.source_armature:
            self.report({'ERROR'}, "Requires a valid source and target armature.")
            return {'CANCELLED'}

        source = bpy.data.objects[self.source_armature]
        bones = (
            [b for b in target.pose.bones if b.bone.select]
            if self.only_selected else target.pose.bones)

        if self.clear_previous:
            self.clear_constraints(bones, source)

        for src_bone in source.pose.bones:
            for tgt_bone in bones:
                if src_bone.name == tgt_bone.name:
                    # remove any existing matching constraint
                    for c in [c for c in tgt_bone.constraints
                              if c.type == self.constraint_type and c.target == source]:
                        tgt_bone.constraints.remove(c)
                    # add new copy constraint
                    c = tgt_bone.constraints.new(self.constraint_type)
                    c.target = source
                    c.subtarget = src_bone.name

        if self.apply_visual_transform:
            bpy.ops.pose.select_all(action='SELECT')
            bpy.ops.pose.visual_transform_apply()
            self.clear_constraints(bones, source)
            self.report({'INFO'}, "Visual transforms applied; constraints removed.")
        else:
            self.report({'INFO'},"{} constraints applied.".format(self.constraint_type.replace('_', ' ').title()))

        context.scene.update_tag()
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "source_armature")
        layout.prop(self, "constraint_type")
        layout.prop(self, "apply_visual_transform")
        layout.prop(self, "only_selected")
        layout.prop(self, "clear_previous")

# ------------------------------------------------------------
# Operator for Locking the Hip on Z axis
# ------------------------------------------------------------

VALID_HIP_NAMES = ("HipN", "Hip", "bip_pelvis", "ValveBiped.Bip01_Pelvis", "Bip01_Pelvis", "Pelvis")

class SSB_OT_LockHip(bpy.types.Operator):
    bl_idname = "ssb4.lock_hip"
    bl_label = "Lock Hip"
    bl_description = ("Creates a constraint to lock the Hip bone's current Z position. (Useful for IK solving)")

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            return False
        # Check for existing constraint
        for candidate in VALID_HIP_NAMES:
            if candidate in obj.pose.bones:
                pbone = obj.pose.bones[candidate]
                for c in pbone.constraints:
                    if c.name == "Hip Z Axis Lock":
                        return False
        return True

    def execute(self, context):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}

        # Determine which hip bone is available; prefer "HipN", then "Hip"
        hip_bone_name = None
        for candidate in VALID_HIP_NAMES:
            if candidate in obj.data.bones:
                hip_bone_name = candidate
                break

        if not hip_bone_name:
            self.report({'ERROR'}, "No hip bone (HipN/Hip) found!")
            return {'CANCELLED'}

        # Get the pose bone
        try:
            pbone = obj.pose.bones[hip_bone_name]
        except KeyError:
            self.report({'ERROR'}, "No pose bone for '%s'" % hip_bone_name)
            return {'CANCELLED'}

        # --- Remove any existing hip lock first ---
        # Remove constraint if present
        for constr in pbone.constraints:
            if constr.name == "Hip Z Axis Lock":
                pbone.constraints.remove(constr)
                break

        # Delete the existing empty if it exists
        empty_name = "Lock_" + hip_bone_name
        if empty_name in bpy.data.objects:
            empty = bpy.data.objects[empty_name]
            bpy.context.scene.objects.unlink(empty)
            bpy.data.objects.remove(empty)

        # --- Create a new lock ---
        # Compute world location of the hip bone (using its head)
        hip_world_loc = obj.matrix_world * pbone.head

        # Create a new empty object at that location
        empty = bpy.data.objects.new(empty_name, None)
        empty.empty_draw_type = 'PLAIN_AXES'
        empty.location = hip_world_loc

        # Link the empty to the scene (Blender 2.79 API)
        bpy.context.scene.objects.link(empty)
        # Hide the empty in viewport and render
        empty.hide = True
        empty.hide_render = True

        # Add a Copy Location constraint to the pose bone that only affects the Z axis in world space
        constr = pbone.constraints.new('COPY_LOCATION')
        constr.name = "Hip Z Axis Lock"
        constr.target = empty
        constr.use_x = False
        constr.use_y = False
        constr.use_z = True
        constr.owner_space = 'WORLD'
        constr.target_space = 'WORLD'

        self.report({'INFO'}, "Locked hip bone '%s' using empty '%s'" % (hip_bone_name, empty_name))
        return {'FINISHED'}

    def invoke(self, context, event):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':  # Ensure an armature is selected before unlocking
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}

        # Clear existing lock only if it's active
        for candidate in VALID_HIP_NAMES:
            if candidate in obj.pose.bones:
                pbone = obj.pose.bones[candidate]
                for c in pbone.constraints:
                    if c.name == "Hip Z Axis Lock":
                        bpy.ops.ssb4.unlock_hip('EXEC_DEFAULT')
                        break
        return self.execute(context)

class SSB_OT_UnlockHip(bpy.types.Operator):
    bl_idname = "ssb4.unlock_hip"
    bl_label = "Unlock Hip"
    bl_description = "Removes the hip lock constraint and deletes the associated empty object"

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            return False
        for candidate in VALID_HIP_NAMES:
            if candidate in obj.pose.bones:
                pbone = obj.pose.bones[candidate]
                for c in pbone.constraints:
                    if c.name == "Hip Z Axis Lock":
                        return True
        return False

    def execute(self, context):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}
        
        # Determine which hip bone is available; prefer "HipN", then "Hip"
        hip_bone_name = None
        for candidate in VALID_HIP_NAMES:
            if candidate in obj.data.bones:
                hip_bone_name = candidate
                break
        if not hip_bone_name:
            self.report({'ERROR'}, "No hip bone (HipN/Hip) found!")
            return {'CANCELLED'}
        
        # Get the pose bone
        try:
            pbone = obj.pose.bones[hip_bone_name]
        except KeyError:
            self.report({'ERROR'}, "No pose bone for '%s'" % hip_bone_name)
            return {'CANCELLED'}
        
        # Remove the constraint named "Hip Z Axis Lock" if it exists
        for constr in pbone.constraints:
            if constr.name == "Hip Z Axis Lock":
                pbone.constraints.remove(constr)
                break
        
        # Look for an object (empty) named "HipLock_<hip_bone_name>" and delete it
        empty_name = "Lock_" + hip_bone_name
        if empty_name in bpy.data.objects:
            empty = bpy.data.objects[empty_name]
            # Remove the empty from the scene
            bpy.context.scene.objects.unlink(empty)
            bpy.data.objects.remove(empty)
        
        self.report({'INFO'}, "Unlocked hip bone '%s'" % hip_bone_name)
        return {'FINISHED'}

# ------------------------------------------------------------
# Operator for Clearing Location Keyframes and Resetting Location
# ------------------------------------------------------------
class SSB_OT_ClearLocationKeyframes(bpy.types.Operator):
    """
    Clears location keyframes and resets location for non-immune bones.
    Immune bones: TransN/Trans, RotN/Rot, HipN/Hip (unless 'Clear Hip' is enabled).
    """
    bl_idname = "ssb4.clear_location_keyframes"
    bl_label = "Clear Location Keyframes"
    bl_description = "Clear location keyframes and reset location offset for all deform pose bones"

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def execute(self, context):
        obj = context.scene.objects.active
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected!")
            return {'CANCELLED'}

        armature = obj.data
        target_bone_names = {bone.name for bone in get_target_bones(armature)}

        immune = {"TransN", "Trans", "RotN", "Rot"}
        if not context.scene.ssb4_clear_hip:
            # Hip bone is immune if "Clear Hip" is toggled
            immune.update(VALID_HIP_NAMES)

        # Remove location keyframes
        if obj.animation_data and obj.animation_data.action:
            action = obj.animation_data.action
            remove_indices = []
            for i, fc in enumerate(action.fcurves):
                if fc.data_path.startswith('pose.bones["') and '.location' in fc.data_path:
                    try:
                        bone_name = fc.data_path.split('["')[1].split('"]')[0]
                    except Exception:
                        continue
                    if bone_name in target_bone_names and bone_name not in immune:
                        remove_indices.append(i)
            for i in reversed(remove_indices):
                action.fcurves.remove(action.fcurves[i])

        # Zero out location
        for pbone in obj.pose.bones:
            if pbone.name in target_bone_names and pbone.name not in immune:
                pbone.location = (0.0, 0.0, 0.0)

        self.report({'INFO'}, "Cleared location keyframes and reset location for non-immune bones")
        return {'FINISHED'}

# ------------------------------------------------------------
# smash-ultimate-blender Bone Simulator
# ------------------------------------------------------------

# Global dictionary to store original bone lengths per armature.
ORIGINAL_BONE_DATA = {}

def is_real_ssbu_null(bone):
    """Return True if this bone is a genuine SSBU null-swing (S_*_null whose parent is also S_* but not _null)."""
    name = bone.name
    parent = bone.parent
    return (
        name.startswith("S_") and
        name.endswith("_null") and
        parent and
        parent.name.startswith("S_") and
        not parent.name.endswith("_null")
    )

def are_vectors_close(a: Vector, b: Vector, tol: float = 1e-5) -> bool:
    """Return True if two vectors are equal within the given absolute tolerance."""
    return all(math.isclose(a[i], b[i], abs_tol=tol) for i in range(3))

class SSB_OT_UltiBones(bpy.types.Operator):
    bl_idname = "ssb4.ulti_bones"
    bl_label  = "Convert to smash-ultimate-blender"
    bl_description = (
        "Convert skeleton to match smash-ultimate-blender plugin "
        "(rotates local Z axis -90° & adjusts bone lengths)"
    )

    @classmethod
    def poll(cls, context):
        scene = context.scene
        obj   = scene.objects.active
        # Must be an armature
        if not obj or obj.type != 'ARMATURE':
            return False
        # Override bypasses the “already converted” check
        if scene.ssb4_override_lock:
            return True
        # Otherwise only if not already snapshot
        return obj.as_pointer() not in ORIGINAL_BONE_DATA


    def execute(self, context):
        scene = context.scene
        obj   = scene.objects.active
        old_scheme = scene.ssb4_last_scheme

        # 1) Force SSBU naming so our snapshot matches what we're about to rotate
        bpy.ops.ssb4.convert_to_ssbu(
            'EXEC_DEFAULT',
            character=scene.ssb4_character,
            force=True)
        scene.ssb4_last_scheme = old_scheme

        # 2) Go into EDIT and snapshot head/tail/roll of every bone
        arm_key = obj.as_pointer()
        bpy.ops.object.mode_set(mode='EDIT')
        arm = obj.data
        edit_bones = arm.edit_bones

        ORIGINAL_BONE_DATA[arm_key] = {
            eb.name: {
                'head': eb.head.copy(),
                'tail': eb.tail.copy(),
                'roll': eb.roll }
            for eb in edit_bones }
        # Also remember what scheme to rename back to
        ORIGINAL_BONE_DATA[arm_key]['last_scheme'] = old_scheme

        # 3) Apply the −90° Z rotation
        rot_z = Matrix.Rotation(math.radians(-90), 4, 'Z')
        for eb in arm.edit_bones:
            eb.matrix = eb.matrix * rot_z

        # 4) Adjust lengths per hierarchy, set all bones to a length of 1 by default
        for eb in edit_bones:

            # # DEBUG: print classification
            # print("{name:30s} | is_real={real} | children={count} | parent={parent}".format(
                # name=eb.name,
                # real=is_real_ssbu_null(eb),
                # count=len(eb.children),
                # parent=(eb.parent.name if eb.parent else "None")
            # ))

            eb.length = 1.0
            name = eb.name
            children = eb.children

            # Real SSBU null-swing bones and leaf bones always inherit their parent’s length
            if eb.parent and (
                   is_real_ssbu_null(eb)
                or (len(eb.children) == 0 and not eb.name.endswith("_null"))):
                eb.length = eb.parent.length
                continue

            # Single-child chain → stretch to match child distance (skip if heads coincide)
            if len(children) == 1:
                child = children[0]
                if not are_vectors_close(eb.head, child.head):
                    eb.length = (child.head - eb.head).length
                continue

            # Multi-child “_eff” helper → length to that eff-child (should not appear)
            for c in children:
                if c.name == name + "_eff":
                    eb.length = (c.head - eb.head).length
                    break

            # Finger base bones → match next segment
            finger_bases = ["FingerL10","FingerL20","FingerL30","FingerL40",
                            "FingerR10","FingerR20","FingerR30","FingerR40"]
            if name in finger_bases:
                next_bone = edit_bones.get(name[:-1] + "1")
                if next_bone:
                    eb.length = (next_bone.head - eb.head).length
                    continue

            # Special cases for limbs
            target = None
            if name == "ClavicleC":
                target = "Neck"
            else:
                for src, tgt in (("Arm",      "Hand"),
                                 ("Shoulder","Arm"),
                                 ("Leg",      "Knee"),
                                 ("Knee",     "Foot")):
                    if name == src + "L" or name == src + "R":
                        target = tgt + name[-1]
                        break

            if target:
                other = edit_bones.get(target)
                if other:
                    eb.length = (other.head - eb.head).length
                continue

        bpy.ops.object.mode_set(mode='OBJECT')
        self.report({'INFO'}, "Converted %s to smash-ultimate-blender" % obj.name)
        return {'FINISHED'}

class SSB_OT_RevertUltiBones(bpy.types.Operator):
    bl_idname = "ssb4.revert_ulti_bones"
    bl_label  = "Revert from smash-ultimate-blender"
    bl_description = (
        "Revert smash-ultimate-blender skeleton changes. "
        "(Restores original head/tail/roll, keeps Z-up orientation)"
    )

    @classmethod
    def poll(cls, context):
        scene = context.scene
        obj   = scene.objects.active
        # Must be an armature
        if not obj or obj.type != 'ARMATURE':
            return False
        # Override bypasses the “nothing to revert” check
        if scene.ssb4_override_lock:
            return True
        # Otherwise only if there *is* a snapshot
        return obj.as_pointer() in ORIGINAL_BONE_DATA


    def execute(self, context):
        scene = context.scene
        obj   = scene.objects.active
        arm_key = obj.as_pointer()

        data = ORIGINAL_BONE_DATA.get(arm_key)
        if not data:
            self.report({'ERROR'}, "No conversion to revert on %s" % obj.name)
            return {'CANCELLED'}

        # 1) Normalize to SSBU naming so bone names match our snapshot keys
        ok, _, _ = rename_bones(
            scene.ssb4_character,
            target_format='SSBU',
            ignore_scope=True)
        if not ok:
            self.report({'ERROR'}, "Failed to normalize to SSBU before revert")
            return {'CANCELLED'}

        # 2) Restore head/tail/roll exactly
        bpy.ops.object.mode_set(mode='EDIT')
        arm = obj.data
        edit_bones = arm.edit_bones

        for eb in edit_bones:
            orig = data.get(eb.name)
            if orig:
                eb.head = orig['head']
                eb.tail = orig['tail']
                eb.roll = orig['roll']

        bpy.ops.object.mode_set(mode='OBJECT')

        # 3) Rename back to original scheme
        last = data['last_scheme']
        if last in ('SSB4','SSBU'):
            rename_bones(scene.ssb4_character, target_format=last, ignore_scope=True)
        else:
            prefs = bpy.context.user_preferences.addons[__name__].preferences
            rename_bones(
                scene.ssb4_character,
                target_format=prefs.valve_bone_format,
                ignore_scope=True)

        # 4) Clean up
        del ORIGINAL_BONE_DATA[arm_key]
        self.report({'INFO'}, "Reverted smash-ultimate conversion on {obj.name}")
        return {'FINISHED'}

# ------------------------------------------------------------
# Operator: Group Bones into Collections and Assign Colors
# ------------------------------------------------------------
class SSB_OT_GroupBones(bpy.types.Operator):
    """Group bones according to bonemap prefixes and patterns"""
    bl_idname = "ssb4.group_bones"
    bl_label = "Group Bones"
    bl_description = "Assign bones to groups based on naming rules and bonemap info"

    @classmethod
    def poll(cls, context):
        obj = context.scene.objects.active
        return obj and obj.type == 'ARMATURE'

    def execute(self, context):
        arma = context.scene.objects.active
        pg = arma.pose.bone_groups

        # --- Group structure and colors ---
        group_order = [
            "Standard Bones",
            "Finger Bones",
            "Helper Bones",
            '"Exo" Helper Bones',
            "Swing Bones",
            "Null Swing Bones",
            "System Bones",
            "Empty Bones",
        ]
        group_colors = {
            "Standard Bones":     'DEFAULT',
            "Finger Bones":       'THEME07',
            "Helper Bones":       'THEME06',
            '"Exo" Helper Bones': 'THEME09',
            "Swing Bones":        'THEME04',
            "Null Swing Bones":   'THEME10',
            "System Bones":       'THEME10',
            "Empty Bones":       'THEME01',
        }
        buckets = { name: [] for name in group_order }
        pre_existing = { name: (pg.get(name) is not None) for name in group_order }

        # --- Bonemap + weight helper sets ---
        _, _, common_maps = CHARACTER_BONE_MAPS.get("Common", (False, False, []))
        _, _, char_maps   = CHARACTER_BONE_MAPS.get(context.scene.ssb4_character, (False, False, []))
        maps = common_maps + char_maps

        all_mapped_names = set()
        for entry in maps:
            all_mapped_names.update(entry)

        def get_bonemap_entry(bone_name):
            if bone_name in all_mapped_names:
                return next((e for e in maps if bone_name in e), None)
            if bone_name.startswith("ValveBiped.") and bone_name[len("ValveBiped."):] in all_mapped_names:
                return next((e for e in maps if bone_name[len("ValveBiped."):] in e), None)
            return None

        def has_vertex_weights(bone_name):
            for obj in bpy.data.objects:
                if obj.type != 'MESH':
                    continue
                if bone_name in obj.vertex_groups:
                    vg_idx = obj.vertex_groups[bone_name].index
                    for v in obj.data.vertices:
                        if any(g.group == vg_idx for g in v.groups):
                            return True
            return False

        # --- Bone classification ---
        system_names = {'TransN','Trans','RotN','Rot','ThrowN','Throw'}
        system_suffixes = ('_null','_eff','_offset')
        assigned = set()

        for pb in arma.pose.bones:
            name = pb.name
            entry = get_bonemap_entry(name)
            canon = entry[3] if entry else None

            # _null/_shit detection first
            if name.endswith('_null') or name.endswith('_shit') or (
                entry and any(n.endswith('_null') or n.endswith('_shit') for n in entry)
            ):
                buckets["Null Swing Bones"].append(pb)
                assigned.add(name)
                continue

            # Swing detection (SSB4 SWG_*__swing or SSBU S_*)
            if name.startswith("SWG_") or name.endswith("_swing") or name.startswith("S_"):
                buckets["Swing Bones"].append(pb)
                assigned.add(name)
                continue

            if entry:
                if canon and 'finger' in canon.lower():
                    buckets["Finger Bones"].append(pb)
                elif canon and canon.startswith("H_Exo_"):
                    buckets['"Exo" Helper Bones'].append(pb)
                elif canon and canon.startswith("H_"):
                    buckets["Helper Bones"].append(pb)
                elif canon and (canon in system_names or canon.endswith(system_suffixes)):
                    buckets["System Bones"].append(pb)
                else:
                    buckets["Standard Bones"].append(pb)
                assigned.add(name)
                continue

        # --- Final pass: Empty Bones (unmapped, unweighted) ---
        for pb in arma.pose.bones:
            name = pb.name
            if name in assigned:
                continue
            if name in all_mapped_names or (
                name.startswith("ValveBiped.") and name[len("ValveBiped."):] in all_mapped_names
            ):
                continue
            if not has_vertex_weights(name):
                buckets["Empty Bones"].append(pb)
                assigned.add(name)

        # --- Apply bone groups ---
        created = 0
        assigned_count = 0
        for group in group_order:
            bones = buckets[group]
            if not bones:
                continue
            if not pre_existing[group]:
                created += 1
            grp = pg.get(group) or pg.new(group)
            grp.color_set = group_colors[group]
            for pb in bones:
                if not pb.bone_group or pb.bone_group.name != group:
                    pb.bone_group = grp
                    assigned_count += 1

        self.report(
            {'INFO'},
            "Created %d new groups; assigned %d bones to groups on %s" %
            (created, assigned_count, arma.name))
        return {'FINISHED'}

# ------------------------------------------------------------
# Addon Preferences for Panel Location
# ------------------------------------------------------------
def update_panel_location(self, context):
    try:
        bpy.utils.unregister_class(SSB_PT_RenamingPanel)
    except Exception:
        pass
    SSB_PT_RenamingPanel.bl_category = self.panel_category
    bpy.utils.register_class(SSB_PT_RenamingPanel)

class SSB_RenamerPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    panel_category = bpy.props.EnumProperty(
        name="Panel Location",
        description="Choose where the tool panel appears",
        items=[
            ('Smash Bones', "Smash Bones", "Display in its own tab in the Tool Shelf"),
            ('Tools', "Tools", "Display under tab 'Tools' in the Tool Shelf"),
            ('Animation', "Animation", "Display under tab 'Animation' in the Tool Shelf"),
            ('Relations', "Relations", "Display under tab 'Relations' in the Tool Shelf"),
            ('Name', "Name", "Display under tab 'Name' in the Tool Shelf"),
            ('Misc', "Misc", "Display under tab 'Misc' in the Tool Shelf"),
        ], default='Smash Bones', update=update_panel_location)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "panel_category")

# ------------------------------------------------------------
# Addon Preferences for Valve Bone Names
# ------------------------------------------------------------
class SSB_ValveBonePreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    valve_bone_format = bpy.props.EnumProperty(
        name='Valve Bone Names',
        description='Choose Valve format',
        items=[
            ('HL2', 'Half Life 2  /  ValveBiped.Bip01', 'Use Half Life 2/L4D/Gmod names (bonemap index 0)'),
            ('TF2', 'Team Fortress 2  /  bip_ + prp_', 'Use Team Fortress 2 names (bonemap index 1)')
        ], default='TF2')

    trim_valvebiped = bpy.props.BoolProperty(
        name="Trim ValveBiped.",
        description="Removes the ValveBiped. prefix from HL2 bone names",
        default=False)

    convert_to_valve_script = bpy.props.BoolProperty(
        name="Convert to Valve creates QC script",
        description="'Convert to Valve' will generate a QC script in the Text Editor instead of renaming bones",
        default=False)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'valve_bone_format')
        row = layout.row(align=True)
        row.prop(self, 'trim_valvebiped')
        row.prop(self, 'convert_to_valve_script')

# ------------------------------------------------------------
# The Panel UI Layout
# ------------------------------------------------------------
class SSB_PT_RenamingPanel(bpy.types.Panel):
    bl_label = "Super Smash Bones"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'TOOLS'
    bl_category = "Smash Bones"
    
    # Draw header with custom icon if available
    def draw_header(self, context):
        layout = self.layout
        layout.label

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Bone scope selection with an icon
        layout.label(text="Target Bones:", icon="BONE_DATA")
        layout.prop(scene, "ssb4_scope", expand=True)
        layout.separator()

        # Active Fighter section – label appears above the drop-down
        layout.label(text="Active Fighter:", icon_value=custom_icons["SMASH_ICON"].icon_id)
        layout.prop(scene, "ssb4_character", text="")

        # Rename Buttons
        row = layout.row(align=True)
        row.alignment = 'CENTER'
        row.operator("ssb4.convert_to_ssb4", text="    Convert to SSB4")
        row = layout.row(align=True)
        row.alignment = 'CENTER'
        row.operator("ssb4.convert_to_ssbu", text="    Convert to SSBU")
        row = layout.row(align=True)
        row.alignment = 'CENTER'
        row.operator("ssb4.convert_to_valve", text="    Convert to Valve")
        layout.separator()
        layout.prop(scene, "ssb4_rename_swing")
        layout.prop(scene, "ssb4_rename_null_swing")

        # Trim Bone Names section
        layout.label(text="Trim Bone Names:", icon="PARTICLEMODE")
        layout.prop(scene, "ssb4_direction", expand=True)  # Start/End toggle
        row = layout.row(align=True)
        row.operator("ssb4.trim_string", text="       TRIM")
        row.prop(scene, "ssb4_count", text="")  # Display number only
        layout.separator()

        # Find & Replace section
        layout.label(text="Find & Replace:", icon="VIEWZOOM")
        row = layout.row()
        row.prop(scene, "ssb4_find", text="Find")
        row = layout.row()
        row.prop(scene, "ssb4_replace", text="Replace")
        layout.operator("ssb4.find_replace_bones", text="Rename Bones", icon="FILE_REFRESH")
        layout.operator("ssb4.strip_nuanmx", text="Strip .NUANMX", icon="SORTALPHA")
        layout.separator()
        
        # Retargeting Tools section
        layout.label(text="Lazy Retargeting:", icon="CURSOR")
        layout.operator("ssb4.rotate_trans90", text="Rotate Up 90°", icon="FILE_PARENT")
        layout.operator("pose.copy_transforms_from_other", text="Pose Bone Transforms", icon="CONSTRAINT")
        row = layout.row(align=True)
        row.operator("ssb4.lock_hip", text="Z-Lock Hip", icon="LOCKED")
        row.operator("ssb4.unlock_hip", text="Unlock Hip", icon="UNLOCKED")
        row.alignment = 'CENTER'
        layout.operator("ssb4.clear_location_keyframes", text="Clear Location Keyframes", icon="KEY_DEHLT")
        layout.prop(scene, "ssb4_clear_hip")

        # smash-ultimate-blender bone buttons
        layout.separator()
        layout.label(text="smash-ultimate-blender", icon="ARMATURE_DATA")
        row = layout.row(align=True)
        row.operator("ssb4.ulti_bones", text="Convert")
        row.operator("ssb4.revert_ulti_bones", text="Revert")
        layout.operator("ssb4.group_bones", text="Group Bones", icon="GROUP_BONE")
        layout.prop(context.scene, "ssb4_override_lock", text="Override Lock")


# ------------------------------------------------------------
# Registration
# ------------------------------------------------------------
classes = (
    SSB_OT_ConvertToSSBU,
    SSB_OT_ConvertToSSB4,
    SSB_OT_ConvertToValve,
    SSB_OT_TrimString,
    SSB_OT_FindReplaceBones,
    SSB_OT_StripNuanmxFromActions,
    SSB_OT_Rotate90,
    SSB_OT_PoseBoneTransforms,
    SSB_OT_LockHip,
    SSB_OT_UnlockHip,
    SSB_OT_ClearLocationKeyframes,
    SSB_OT_UltiBones,
    SSB_OT_RevertUltiBones,
    SSB_OT_GroupBones,
    SSB_RenamerPreferences,
    SSB_ValveBonePreferences,
    SSB_PT_RenamingPanel,
)

def register():
    load_custom_icons()
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as e:
            print("Could not unregister %r: %s" % (cls, e))
    unload_custom_icons()

if __name__ == "__main__":
    register()
