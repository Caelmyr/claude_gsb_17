"""
图谱存储模块 - JSON文件分片存储
"""
import json
import os
import threading
from typing import Dict, List, Optional
from backend.utils.config import GRAPH_DIR, GRAPH_SHARDS


class GraphStorage:
    """图谱存储管理器 - 按实体类型分片"""

    def __init__(self):
        # add_relation 会在同一把锁内调用 add_entity，因此使用可重入锁
        self.lock = threading.RLock()
        self._ensure_directories()
        self._cache = {}
        self._load_all_shards()

    def _ensure_directories(self):
        """确保目录存在"""
        os.makedirs(GRAPH_DIR, exist_ok=True)

    def _load_all_shards(self):
        """加载所有分片到缓存"""
        for entity_type, filename in GRAPH_SHARDS.items():
            filepath = os.path.join(GRAPH_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    self._cache[entity_type] = json.load(f)
            else:
                self._cache[entity_type] = {'entities': {}, 'relations': []}

    def _normalize_type(self, entity_type: str) -> str:
        """将外部实体类型归一化到当前系统支持的分片类型"""
        if not entity_type:
            return 'OTHER'

        entity_type = str(entity_type).strip().upper()
        return entity_type if entity_type in GRAPH_SHARDS else 'OTHER'

    def _save_shard(self, entity_type: str):
        """原子性保存指定分片到文件"""
        entity_type = self._normalize_type(entity_type)
        filename = GRAPH_SHARDS.get(entity_type, 'other.json')
        filepath = os.path.join(GRAPH_DIR, filename)
        temp_filepath = f'{filepath}.tmp'

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(self._cache[entity_type], f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)

    def _get_entity_record(self, entity_text: str) -> Optional[Dict]:
        """在缓存中查找实体，不加锁"""
        for shard in self._cache.values():
            entity = shard['entities'].get(entity_text)
            if entity:
                return entity
        return None

    def _merge_entity_locked(self, entity_text: str, entity_type: str,
                             properties: Dict = None, count: int = 1) -> Dict:
        """新增或合并实体。调用方必须已经持有锁。"""
        entity_type = self._normalize_type(entity_type)
        properties = properties or {}
        count = max(1, int(count or 1))

        existing = self._get_entity_record(entity_text)
        if existing:
            existing['count'] = existing.get('count', 1) + count
            existing.setdefault('properties', {}).update(properties)
            return existing

        if entity_type not in self._cache:
            self._cache[entity_type] = {'entities': {}, 'relations': []}

        entity = {
            'id': f"{entity_type}_{len(self._cache[entity_type]['entities'])}",
            'text': entity_text,
            'type': entity_type,
            'properties': properties,
            'count': count
        }
        self._cache[entity_type]['entities'][entity_text] = entity
        return entity

    def add_entity(self, entity_text: str, entity_type: str, properties: Dict = None):
        """添加实体"""
        with self.lock:
            record = self._merge_entity_locked(entity_text, entity_type, properties)
            self._save_shard(record['type'])

    def add_relation(self, subject: str, subject_type: str, predicate: str,
                     obj: str, object_type: str, properties: Dict = None):
        """添加关系"""
        with self.lock:
            subject_record = self._merge_entity_locked(subject, subject_type)
            object_record = self._merge_entity_locked(obj, object_type)

            subject_type = subject_record['type']
            object_type = object_record['type']
            relation = {
                'subject': subject,
                'subject_type': subject_type,
                'predicate': predicate,
                'object': obj,
                'object_type': object_type,
                'properties': properties or {}
            }

            # 检查是否已存在
            existing_relations = self._cache[subject_type]['relations']
            exists = any(
                r['subject'] == subject and
                r['predicate'] == predicate and
                r['object'] == obj
                for r in existing_relations
            )
            if not exists:
                existing_relations.append(relation)
            self._save_shard(subject_type)
            if object_type != subject_type:
                self._save_shard(object_type)

    def import_graph(self, entities: List[Dict], relations: List[Dict]) -> Dict:
        """
        批量导入已经完成格式校验的实体和关系。

        实体按 text 去重；关系按 (subject, predicate, object) 去重。
        重复数据的 properties 深度一层合并，count 累加。
        """
        summary = {
            'entities_added': 0,
            'entities_merged': 0,
            'relations_added': 0,
            'relations_merged': 0,
            'type_conflicts': 0
        }
        touched_shards = set()

        with self.lock:
            # 先合并实体，确保关系端点类型与已存在实体保持一致
            for entity in entities:
                text = entity['text']
                incoming_type = self._normalize_type(entity.get('type', 'OTHER'))
                existed = self._get_entity_record(text) is not None

                record = self._merge_entity_locked(
                    text,
                    incoming_type,
                    entity.get('properties') or {},
                    entity.get('count', 1)
                )
                touched_shards.add(record['type'])

                if existed:
                    summary['entities_merged'] += 1
                    if self._normalize_type(incoming_type) != record['type']:
                        summary['type_conflicts'] += 1
                else:
                    summary['entities_added'] += 1

            for relation in relations:
                subject = relation['subject']
                obj = relation['object']
                predicate = relation['predicate']

                subject_record = self._get_entity_record(subject)
                object_record = self._get_entity_record(obj)
                if not subject_record or not object_record:
                    # 正常情况下 exchange 层会补齐端点，这里用于防止绕过格式解析
                    continue

                subject_type = subject_record['type']
                object_type = object_record['type']
                shard_relations = self._cache[subject_type]['relations']
                existing_relation = next((
                    r for r in shard_relations
                    if r['subject'] == subject and
                    r['predicate'] == predicate and
                    r['object'] == obj
                ), None)

                if existing_relation:
                    existing_relation.setdefault('properties', {}).update(
                        relation.get('properties') or {}
                    )
                    existing_relation['count'] = (
                        existing_relation.get('count', 1) +
                        max(1, int(relation.get('count', 1)))
                    )
                    summary['relations_merged'] += 1
                else:
                    shard_relations.append({
                        'subject': subject,
                        'subject_type': subject_type,
                        'predicate': predicate,
                        'object': obj,
                        'object_type': object_type,
                        'properties': relation.get('properties') or {},
                        'count': max(1, int(relation.get('count', 1)))
                    })
                    summary['relations_added'] += 1

                touched_shards.add(subject_type)

            for shard_type in touched_shards:
                self._save_shard(shard_type)

        return summary

    def get_entity(self, entity_text: str) -> Optional[Dict]:
        """获取实体信息"""
        return self._get_entity_record(entity_text)

    def get_entity_relations(self, entity_text: str) -> List[Dict]:
        """获取实体的所有关系"""
        relations = []
        for entity_type, shard in self._cache.items():
            for relation in shard['relations']:
                if relation['subject'] == entity_text or relation['object'] == entity_text:
                    relations.append(relation)
        return relations

    def get_all_entities(self) -> List[Dict]:
        """获取所有实体"""
        entities = []
        for entity_type, shard in self._cache.items():
            entities.extend(shard['entities'].values())
        return entities

    def get_all_relations(self) -> List[Dict]:
        """获取所有关系"""
        relations = []
        for entity_type, shard in self._cache.items():
            relations.extend(shard['relations'])
        return relations

    def get_graph_data(self) -> Dict:
        """获取图谱可视化数据"""
        nodes = []
        links = []
        node_ids = set()

        for entity_type, shard in self._cache.items():
            for entity_text, entity_data in shard['entities'].items():
                if entity_data['id'] not in node_ids:
                    node_ids.add(entity_data['id'])
                    nodes.append({
                        'id': entity_data['id'],
                        'label': entity_text,
                        'type': entity_type,
                        'count': entity_data.get('count', 1)
                    })

            for relation in shard['relations']:
                source_entity = self.get_entity(relation['subject'])
                target_entity = self.get_entity(relation['object'])
                if source_entity and target_entity:
                    links.append({
                        'source': source_entity['id'],
                        'target': target_entity['id'],
                        'label': relation['predicate']
                    })

        return {'nodes': nodes, 'links': links}

    def search_entities(self, keyword: str) -> List[Dict]:
        """搜索实体"""
        results = []
        for entity_type, shard in self._cache.items():
            for entity_text, entity_data in shard['entities'].items():
                if keyword in entity_text:
                    results.append(entity_data)
        return results

    def get_statistics(self) -> Dict:
        """获取图谱统计信息"""
        total_entities = 0
        total_relations = 0
        entity_counts = {}

        for entity_type, shard in self._cache.items():
            count = len(shard['entities'])
            entity_counts[entity_type] = count
            total_entities += count
            total_relations += len(shard['relations'])

        return {
            'total_entities': total_entities,
            'total_relations': total_relations,
            'entity_counts': entity_counts
        }
