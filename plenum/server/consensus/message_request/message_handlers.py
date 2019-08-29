import logging
from typing import Dict, Any, Optional, Tuple, Callable
from abc import ABCMeta, abstractmethod

from plenum.common.constants import THREE_PC_PREFIX
from plenum.common.event_bus import InternalBus, ExternalBus
from plenum.common.exceptions import MismatchedMessageReplyException, IncorrectMessageForHandlingException
from plenum.common.messages.message_base import MessageBase
from plenum.common.messages.node_messages import MessageReq, MessageRep, \
    LedgerStatus, PrePrepare, ConsistencyProof, Propagate, Prepare, Commit
from plenum.common.txn_util import TxnUtilConfig
from plenum.common.types import f
from plenum.common.util import compare_3PC_keys
from plenum.server.consensus.consensus_shared_data import ConsensusSharedData
from stp_core.common.log import getlogger


class ThreePhaseMessagesHandler(metaclass=ABCMeta):
    fields = {
        'inst_id': f.INST_ID.nm,
        'view_no': f.VIEW_NO.nm,
        'pp_seq_no': f.PP_SEQ_NO.nm
    }
    msg_cls = NotImplemented

    def __init__(self,
                 data: ConsensusSharedData):
        super().__init__()
        self._data = data
        # Tracks for which keys 'self.msg_cls' have been requested.
        # Cleared in `gc`
        self.requested_messages = {}  # Dict[Tuple[int, int], Optional[Tuple[str, str, str]]]
        self._logger = logging.getLogger()

    @abstractmethod
    def _get_reply(self, params: Dict[str, Any]) -> Any:
        pass

    def _validate(self, **kwargs) -> bool:
        return kwargs['inst_id'] == self._data.inst_id and \
               kwargs['view_no'] == self._data.view_no and \
               isinstance(kwargs['pp_seq_no'], int) and \
               kwargs['pp_seq_no'] > 0

    def _create(self, msg: Dict, **kwargs):
        message = self.msg_cls(**msg)
        if message.instId != kwargs['inst_id'] \
                or message.viewNo != kwargs['view_no'] \
                or message.ppSeqNo != kwargs['pp_seq_no']:
            raise MismatchedMessageReplyException
        return message

    def get_3pc_message(self, msg: MessageRep, frm: str):
        params = {}

        for field_name, type_name in self.fields.items():
            params[field_name] = msg.params.get(type_name)
        self._logger.debug('{} received requested msg ({}) from {}'.format(self, msg, frm))
        self._validate_message_rep(**params)
        try:
            return self._create(msg.msg, **params)
        except TypeError:
            raise IncorrectMessageForHandlingException(msg, 'replied message has invalid structure',
                                                       self._logger.warning)
        except MismatchedMessageReplyException:
            raise IncorrectMessageForHandlingException(msg, 'replied message does not satisfy query criteria',
                                                       self._logger.warning)

    def prepare_msg_to_request(self, three_pc_key: Tuple[int, int],
                               stash_data: Optional[Tuple[str, str, str]] = None) -> Optional[Dict]:
        if three_pc_key in self.requested_messages:
            self._logger.debug('{} not requesting {} since already '
                               'requested for {}'.format(self._data.name, self.msg_cls, three_pc_key))
            return
        self.requested_messages[three_pc_key] = stash_data
        return {f.INST_ID.nm: self._data.inst_id,
                f.VIEW_NO.nm: three_pc_key[0],
                f.PP_SEQ_NO.nm: three_pc_key[1]}

    def process_message_req(self, msg: MessageReq):
        params = {}

        for field_name, type_name in self.fields.items():
            params[field_name] = msg.params.get(type_name)

        if not self._validate(**params):
            raise IncorrectMessageForHandlingException(msg, 'cannot serve request',
                                                       self._logger.debug)

        return self._get_reply(params)

    def gc(self):
        self.requested_messages.clear()

    def _validate_message_rep(self, msg: object):
        if msg is None:
            return False, "received null"
        key = (msg.viewNo, msg.ppSeqNo)
        if key not in self.requested_messages:
            return False, 'Had either not requested this msg or already ' \
                          'received the msg for {}'.format(key)
        if self._has_already_ordered(*key):
            return False, 'already ordered msg ({})'.format(self, key)
        # There still might be stashed msg but not checking that
        # it is expensive, also reception of msgs is idempotent
        stashed_data = self.requested_messages[key]
        curr_data = (msg.digest, msg.stateRootHash, msg.txnRootHash) \
            if isinstance(msg, PrePrepare) or isinstance(msg, Prepare) \
            else None
        if stashed_data is None or curr_data == stashed_data:
            return True, None

        raise IncorrectMessageForHandlingException(msg, reason='{} does not have expected state {}'.
                                                   format(THREE_PC_PREFIX, stashed_data),
                                                   logMethod=self._logger.warning)

    def _has_already_ordered(self, view_no, pp_seq_no):
        return compare_3PC_keys((view_no, pp_seq_no),
                                self._data.last_ordered_3pc) >= 0


class PreprepareHandler(ThreePhaseMessagesHandler):
    msg_cls = PrePrepare

    def _get_reply(self, params: Dict[str, Any]) -> Optional[PrePrepare]:
        key = (params['view_no'], params['pp_seq_no'])
        return self._data.sent_preprepares.get(key)

    def _validate_message_rep(self, msg: object):
        result, error_msg = super()._validate_message_rep(msg)
        key = (msg.viewNo, msg.ppSeqNo)
        if result:
            for pp in self._data.preprepared:
                if (pp.view_no, pp.pp_seq_no) == key:
                    return False, 'already received msg ({})'.format(self, key)
        return result, error_msg


class PrepareHandler(ThreePhaseMessagesHandler):
    msg_cls = Prepare

    def _get_reply(self, params: Dict[str, Any]) -> Optional[Prepare]:
        key = (params['view_no'], params['pp_seq_no'])
        if key in self._data.prepares:
            prepare = self._data.prepares[key].msg
            if self._data.prepares.hasPrepareFrom(prepare, self._data.name):
                return prepare
        return None


class CommitHandler(ThreePhaseMessagesHandler):
    msg_cls = Commit

    def _get_reply(self, params: Dict[str, Any]) -> Optional[Commit]:
        key = (params['view_no'], params['pp_seq_no'])
        if key in self._data.commits:
            commit = self._data.commits[key].msg
            if self._data.commits.hasCommitFrom(commit, self._data.name):
                return commit
        return None
